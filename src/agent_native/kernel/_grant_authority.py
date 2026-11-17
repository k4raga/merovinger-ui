"""Closed, callback-free C1 grant owner/lineage predicates.

No app DTOs, author validators, query computations, SQL or cached authority.
Registration uses immutable Registry descriptors. Each check reads one pinned
C1 graph. Structural historical integrity is separate from current usability.
Production promotion is limited to one NEW Activity in the same accepted bundle.
"""
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal
from types import MappingProxyType

from agent_native.schema import EntityKey, Ref, CollectionRef
from agent_native.state import Patch, Update, EntityUnavailable, deep_freeze, thaw, strict_equal, ReadView, StagedReadView, FrozenMap
from agent_native.state.tracking import ReadTracker, ReadSet
from ._system_integrity import SystemIntegrity, SystemIntegrityError
from .permissions import catalog as permission_catalog


class GrantIntegrityError(ValueError):
    """Engine diagnostic only; callers must not export raw authority reasons."""
    def __init__(self, code):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class GrantLimits:
    max_chain: int = 32
    max_resources: int = 1024
    max_work: int = 16384

    def __post_init__(self):
        if any(type(v) is not int or v < 1 for v in (
                self.max_chain, self.max_resources, self.max_work)):
            raise TypeError("Grant limits require positive exact integers")


@dataclass(frozen=True, slots=True)
class ActivityOccurrenceCheck:
    disposition: Literal["nonterminal", "terminal", "absent"]
    read_set: ReadSet


@dataclass(frozen=True, slots=True)
class ActivityCreationEffectCheck:
    activity: Ref
    original_anchor: Ref
    start_invocation: Ref
    start_operation_grant: Ref
    disposition: Literal["nonterminal", "terminal", "absent"]
    read_set: ReadSet
    start_eligibility: Literal["single_start", "multiple_effects"]


@dataclass(frozen=True, slots=True)
class EffectOriginCheck:
    effect: Ref
    invocation: Ref
    source_request: Ref
    task: Ref
    task_generation: int
    operation_grant: Ref
    effect_ids: tuple[str, ...]
    is_new_activity: bool
    read_set: ReadSet


@dataclass(frozen=True, slots=True)
class ActivityBacklinkCheck:
    """A structural field proof and dependencies, never SEND authority."""
    read_set: ReadSet


@dataclass(frozen=True, slots=True)
class ActivityTerminalPatch:
    patch: Patch
    read_set: ReadSet
    disposition: Literal["ended", "already_terminal", "historical_anchor_only"]


@dataclass(frozen=True, slots=True)
class GrantUse:
    allowed: bool
    reason: str | None
    grant: Ref
    owner: object
    chain: tuple[Ref, ...]
    resources: tuple
    read_set: object


def _fail(code):
    raise GrantIntegrityError(code)


def _record(identity):
    return {"entity_type": identity[0], "entity_id": identity[1],
            "generation": identity[2], "collection_field": identity[3]}


def _ref(identity):
    return Ref(EntityKey(*identity[:3]))


class GrantAuthority:
    """Instantiate after declarations; call before StateStore/at host wiring.

    validate_graph(reader): callback-free accepted restore/startup check.
    validate_transition(base, staged): immutable headers, live creation authority,
        same-cut creation proofs and irreversible declared Activity terminal end.
    check_use(reader, grant, ...): GrantUse; inactive/revoked is an ordinary False.
        Structural corruption raises GrantIntegrityError, never a fake empty graph.
    Inputs are raw ReadView/StagedReadView; returned read_set can be carried into
    engine plan tokens. No reader or app values escape in GrantUse.
    """
    def __init__(self, definitions, registries, *, limits=None):
        from .system_definitions import SystemDefinitions, UiSystemDefinitions
        if ((type(definitions) is SystemDefinitions and definitions.registry.revision != 7)
                or (type(definitions) is UiSystemDefinitions and definitions.registry.revision != 1)
                or type(definitions) not in (SystemDefinitions, UiSystemDefinitions)):
            _fail("unsupported_private_grant_schema")
        self.definitions = definitions
        self.limits = GrantLimits() if limits is None else limits
        if type(self.limits) is not GrantLimits:
            raise TypeError("Use typed GrantLimits")
        self.grant_type = definitions.Grant.__entity_type__
        self.task_type = definitions.TaskSession.__entity_type__
        self.invocation_type = definitions.Invocation.__entity_type__
        registries = tuple(registries)
        permission_catalog(registries, {d.definition_id for d in definitions.registry.descriptors(kind="entity")})
        self.manifest = tuple(sorted((registry.namespace, registry.revision,
            tuple((d.definition_id, d.kind, d.revision, d.fingerprint) for d in registry.descriptors()))
            for registry in registries))
        descriptors = {}
        for registry in registries:
            for descriptor in registry.descriptors(kind="entity"):
                if descriptor.definition_id in descriptors:
                    _fail("duplicate_entity_schema")
                descriptors[descriptor.definition_id] = descriptor
        if self.grant_type not in descriptors or self.task_type not in descriptors:
            _fail("missing_shared_grant_schema")
        self.descriptors = MappingProxyType(descriptors)
        activities = {}
        for typ, descriptor in descriptors.items():
            metadata = descriptor.metadata.get("activity")
            if metadata is None:
                continue
            if (not isinstance(metadata, Mapping) or set(metadata) - {
                    "state_field", "active_values", "grant_field", "terminal_values"}
                    or not {"state_field", "active_values", "grant_field"}.issubset(metadata)):
                _fail("invalid_activity_metadata")
            state_field, grant_field = metadata["state_field"], metadata["grant_field"]
            fields = {f.name: f for f in descriptor.fields}
            if (type(state_field) is not str or type(grant_field) is not str
                    or state_field == grant_field or state_field not in fields or grant_field not in fields):
                _fail("invalid_activity_fields")
            relation = fields[grant_field].relation
            if (relation is None or relation.target_type != self.grant_type
                    or relation.kind != "ref" or relation.many):
                _fail("activity_grant_field_must_reference_shared_grant")
            active, terminal = metadata["active_values"], metadata.get("terminal_values", ())
            if (type(active) is not tuple or not active or type(terminal) is not tuple
                    or any(type(x) is not str for x in (*active, *terminal))
                    or len(set(active)) != len(active) or len(set(terminal)) != len(terminal)
                    or set(active) & set(terminal)):
                _fail("invalid_activity_states")
            schema = descriptor.json_schema.get("properties", {}).get(state_field, {})
            if schema.get("type") != "string":
                _fail("activity_state_must_be_string")
            enum = schema.get("enum")
            if enum is not None and not set((*active, *terminal)).issubset(enum):
                _fail("activity_state_not_in_schema")
            activities[typ] = (state_field, tuple(active), grant_field, tuple(terminal))
        self.activities = MappingProxyType(activities)
        self.system_integrity = SystemIntegrity(definitions, registries, self)

    def identity(self, value, *, entity_only=False, grant_only=False):
        if type(value) is Ref:
            key, field = value.key, None
        elif type(value) is CollectionRef:
            key, field = value.owner, value.field
        elif type(value) in (dict, FrozenMap, MappingProxyType) and set(value) == {
                "entity_type", "entity_id", "generation", "collection_field"}:
            try:
                key = EntityKey(value["entity_type"], value["entity_id"], value["generation"])
                field = value["collection_field"]
            except (ValueError, TypeError):
                _fail("malformed_resource_identity")
        else:
            _fail("malformed_resource_identity")
        if key.entity_type not in self.descriptors:
            _fail("unknown_resource_type")
        if field is not None:
            if entity_only or grant_only or type(field) is not str:
                _fail("entity_owner_cannot_be_collection")
            CollectionRef(key, field)
            descriptor = self.descriptors[key.entity_type]
            relation = next((f.relation for f in descriptor.fields if f.name == field), None)
            if relation is None or relation.kind != "owned" or not relation.many or relation.nullable:
                _fail("unsupported_collection_source")
        if grant_only and key.entity_type != self.grant_type:
            _fail("parent_must_be_exact_grant_identity")
        return (key.entity_type, key.entity_id, key.generation, field)

    def _context(self, reader):
        if type(reader) not in (ReadView, StagedReadView):
            raise TypeError("Use a pinned raw C1 reader, not a DTO or mutable store")
        if reader.schema_manifest != self.manifest:
            _fail("grant_schema_manifest_mismatch")
        return _Graph(self, reader)

    def validate_graph(self, reader):
        with self._context(reader) as graph:
            graph.validate()
        try:
            self.system_integrity.validate_graph(reader)
        except SystemIntegrityError as error:
            _fail(error.code)

    def validate_transition(self, base, staged, *, local_attestation=None):
        # C1 structurally shares unchanged immutable Record objects. Compare that
        # single frozen map; only new/changed grants and changed Activity anchors
        # trigger lineage work. Full historical graph validation is startup-only.
        with self._context(base) as old, self._context(staged) as new:
            before, after = base._data().records, staged._data().records
            changed = tuple(key for key, record in after.items() if before.get(key) is not record)
            deleted = tuple(key for key in before if key not in after)
            for key in deleted:
                if key.entity_type in (self.grant_type, self.invocation_type):
                    _fail("authority_proof_deletion_unsupported")
            for key in changed:
                ref = Ref(key)
                previous = before.get(key)
                current = after[key]
                if key.entity_type == self.task_type and previous is not None:
                    if current.values["generation"] < previous.values["generation"]:
                        _fail("task_generation_reversed")
                    if current.values["principal"] != previous.values["principal"]:
                        _fail("task_principal_is_immutable")
                elif key.entity_type == self.invocation_type and previous is not None:
                    for name in ("grant", "plan_id", "principal"):
                        if not strict_equal(previous.values[name], current.values[name]):
                            _fail("invocation_proof_is_immutable")
                elif key.entity_type == self.grant_type:
                    values = new.grant(ref)
                    if previous is not None:
                        prior = old.grant(ref)
                        for name in values:
                            if name != "revoked" and not strict_equal(prior[name], values[name]):
                                _fail("grant_header_is_immutable")
                        if prior["revoked"] and not values["revoked"]:
                            _fail("grant_revocation_is_monotonic")
                    else:
                        for ancestor in new.chain(ref):
                            new.integrity(ancestor)
                        if values["parent"] is not None:
                            parent = _ref(self.identity(values["parent"], grant_only=True))
                            eligible, _ = new.live(parent, strict_task=True)
                            if not eligible:
                                _fail("creation_requires_live_parent")
                        eligible, _ = new.owner_live(ref, strict_task=True)
                        if not eligible:
                            _fail("creation_requires_live_owner")
                        new.creation_proof(ref, require_live=True)
                if key.entity_type in self.activities:
                    spec = self.activities[key.entity_type]
                    backlink = current.values[spec[2]]
                    if previous is not None:
                        if (previous.values[spec[0]] in spec[3]
                                and current.values[spec[0]] != previous.values[spec[0]]):
                            _fail("activity_terminal_is_immutable")
                        previous_anchor = previous.values[spec[2]]
                        if previous_anchor is not None and previous_anchor != backlink:
                            if not new.grant(previous_anchor)["revoked"]:
                                _fail("old_activity_anchor_requires_revocation")
                    if backlink is None:
                        if current.values[spec[0]] in spec[1]:
                            _fail("active_activity_requires_anchor")
                    else:
                        header = new.grant(backlink)
                        if (header["kind"] != "activity"
                                or self.identity(header["owner_activity"], entity_only=True)
                                != self.identity(ref, entity_only=True)):
                            _fail("activity_anchor_backlink_mismatch")
                        new.integrity(backlink)

        try:
            self.system_integrity.validate_transition(base, staged, local_attestation=local_attestation)
        except SystemIntegrityError as error:
            _fail(error.code)

    def check_use(self, reader, grant, *, principal=None, action=None, resource=None):
        if any(value is not None and (type(value) is not str or not value) for value in (principal, action)):
            raise TypeError("Principal/action filters must be exact nonempty strings")
        with self._context(reader) as graph:
            chain = graph.chain(grant)
            for ref in chain:
                graph.integrity(ref)
            eligible, reason = graph.live(grant)
            values = graph.grant(grant)
            if principal is not None and values["principal"] != principal:
                eligible, reason = False, "principal_mismatch"
            if action is not None and action not in values["actions"]:
                eligible, reason = False, "action_outside_grant"
            available = []
            for identity in graph.resource_ids(values["resources"]):
                if graph.resource_live(identity):
                    available.append(deep_freeze(_record(identity)))
            if resource is not None:
                wanted = self.identity(resource)
                if wanted not in graph.resource_ids(values["resources"]):
                    eligible, reason = False, "resource_outside_grant"
                elif not graph.resource_live(wanted):
                    eligible, reason = False, "resource_unavailable"
            owner = (values["owner_activity"] if values["owner_activity"] is not None
                     else _record(self.identity(values["task"], entity_only=True)))
            return GrantUse(eligible, None if eligible else reason, grant, deep_freeze(thaw(owner)
                if type(owner) is not dict else owner), chain, tuple(available) if eligible else (),
                graph.tracker.read_set)

    def check_activity_backlink(self, reader, activity, field, value):
        """Recognize only the current canonical engine-owned scalar backlink.

        None means another slot/value. A malformed claimed canonical slot raises
        GrantIntegrityError. Revoked-but-valid original anchors remain structural
        backlinks. Callers must separately forbid changing this field.
        """
        spec = self.activities.get(activity.key.entity_type if type(activity) is Ref else None)
        if (spec is None or field != spec[2] or type(value) is not Ref
                or value.key.entity_type != self.grant_type):
            return None
        with self._context(reader) as graph:
            header = graph.grant(value)
            plan = header["plan_id"]
            if type(plan) is not str or not plan:
                _fail("noncanonical_activity_backlink")
            invocation = Ref(EntityKey(self.invocation_type, plan, 1))
            try:
                operation = graph.view.field(invocation, "grant")
            except EntityUnavailable:
                _fail("missing_original_start_invocation")
            disposition, _ = graph.original_activity(activity, value, invocation, operation)
            if disposition == "absent":
                _fail("missing_activity_backlink_owner")
            return ActivityBacklinkCheck(graph.tracker.read_set)

    def check_activity_occurrence(self, reader, activity, *, original_anchor,
                                  start_invocation, start_operation_grant):
        """Structural original-start check; never a SEND-authority assertion.

        A revoked but nonterminal occurrence remains nonterminal. Absent means a
        proven historical incarnation, not remote completion. Invalid proof raises.
        """
        with self._context(reader) as graph:
            disposition, _ = graph.original_activity(activity, original_anchor,
                start_invocation, start_operation_grant)
            return ActivityOccurrenceCheck(disposition, graph.tracker.read_set)

    def _effect_origin(self, graph, effect):
        if (type(effect) is not Ref or effect.key.generation != 1
                or effect.key.entity_type != self.definitions.EffectIntent.__entity_type__):
            _fail("invalid_original_start_effect")
        try:
            supplied = graph.view.resolve(effect).values
            invocation, operation = supplied["invocation"], supplied["grant"]
            inv = graph.view.resolve(invocation).values
            request_ref = inv["request"]
            request = graph.view.resolve(request_ref).values
        except EntityUnavailable:
            _fail("missing_original_effect_graph")
        op = graph.grant(operation)
        if (invocation.key.entity_type != self.invocation_type or invocation.key.generation != 1
                or request_ref.key.entity_type != self.definitions.RequestAdmission.__entity_type__
                or request_ref.key.generation != 1 or op["kind"] != "operation"
                or inv["grant"] != operation or inv["task"] != request["task"]
                or inv["principal"] != op["principal"] or inv["plan_id"] != op["plan_id"]
                or op["source_invocation"] is None
                or self.identity(op["source_invocation"], entity_only=True) != self.identity(invocation)
                or invocation.key.entity_id != inv["plan_id"]
                or operation.key.entity_id != inv["plan_id"] + "-grant"):
            _fail("original_effect_origin_mismatch")
        if request["request_id"] != request_ref.key.entity_id:
            _fail("original_effect_task_mismatch")
        if graph.view.exists(inv["task"]):
            task = graph.view.resolve(inv["task"]).values
            if task["principal"] != inv["principal"] or task["generation"] < request["task_generation"]:
                _fail("original_effect_task_mismatch")
        elif graph.birth(inv["task"]) <= graph.birth(operation):
            _fail("original_effect_task_history")
        for ancestor in graph.chain(operation):
            graph.integrity(ancestor)
        identifiers, plan = inv["effect_ids"], inv["plan_id"]
        if (type(identifiers) is not tuple or not identifiers or len(identifiers) > self.limits.max_resources
                or any(type(item) is not str for item in identifiers)
                or len(set(identifiers)) != len(identifiers) or effect.key.entity_id not in identifiers):
            _fail("invalid_original_effect_inventory")
        birth = graph.birth(invocation)
        if not birth or graph.birth(operation) != birth:
            _fail("original_effect_birth_mismatch")
        scope = graph.resource_ids(op["resources"])
        for index, identifier in enumerate(identifiers):
            if identifier != plan + "-effect-" + str(index):
                _fail("noncanonical_original_effect_identity")
            ref = Ref(EntityKey(self.definitions.EffectIntent.__entity_type__, identifier, 1))
            graph.spend()
            try:
                item = graph.view.resolve(ref).values
            except EntityUnavailable:
                _fail("missing_original_inventory_effect")
            resource = self.identity(item["resource"])
            if (item["invocation"] != invocation or item["grant"] != operation
                    or item["operation_id"] != plan + "-operation-" + str(index)
                    or graph.birth(ref) != birth or resource not in scope
                    or resource[0] in self.system_integrity.private_types or item["action"] not in op["actions"]):
                _fail("original_effect_graph_mismatch")
        created = graph.resource_ids(op["created_resources"], entity_only=True)
        is_new = op["owner_activity"] is not None and self.identity(op["owner_activity"], entity_only=True) in created
        return (invocation, request_ref, inv["task"], request["task_generation"], operation, identifiers, is_new)

    def check_effect_origin(self, reader, effect):
        """Whole canonical source graph, including lawful ordinary/existing owners.

        Explicit is_new_activity follows full validation, never an exception
        fallback. The result is provenance with complete reads, not SEND rights.
        """
        with self._context(reader) as graph:
            values = self._effect_origin(graph, effect)
            return EffectOriginCheck(effect, *values, graph.tracker.read_set)

    def check_new_activity_start(self, reader, start_effect):
        """Derive original NEW owner after SAME whole generic origin validation."""
        with self._context(reader) as graph:
            invocation, _, _, _, operation, identifiers, is_new = self._effect_origin(graph, start_effect)
            op = graph.grant(operation)
            if not is_new or op["parent"] is None:
                _fail("start_requires_new_activity_operation")
            activity = _ref(self.identity(op["owner_activity"], entity_only=True))
            anchor = _ref(self.identity(op["parent"], grant_only=True))
            disposition, _ = graph.original_activity(activity, anchor, invocation, operation)
            return ActivityCreationEffectCheck(activity, anchor, invocation, operation,
                disposition, graph.tracker.read_set,
                "single_start" if len(identifiers) == 1 else "multiple_effects")

    def terminal_activity_patch(self, reader, activity, state, *, original_anchor,
                                start_invocation, start_operation_grant):
        """Mechanical engine patch for two separately authenticated caller causes.

        013 proves correlated exact STOP or original-creation NoEffect before use.

        Current pointer/backlink cannot replace original-start identities. Deleted
        A revokes only its original anchor; already-terminal subtype is immutable.
        No live original SEND permission is required to apply an external fact.
        """
        spec = self.activities.get(activity.key.entity_type if type(activity) is Ref else None)
        if spec is None or type(state) is not str or state not in spec[3]:
            _fail("undeclared_activity_terminal_state")
        with self._context(reader) as graph:
            disposition, spec = graph.original_activity(activity, original_anchor,
                start_invocation, start_operation_grant)
            updates = []
            if disposition == "nonterminal":
                updates.append(Update(activity, {spec[0]: state}))
            if not graph.grant(original_anchor)["revoked"]:
                updates.append(Update(original_anchor, {"revoked": True}))
            result = {"nonterminal": "ended", "terminal": "already_terminal",
                      "absent": "historical_anchor_only"}[disposition]
            return ActivityTerminalPatch(Patch(updates=tuple(updates)), graph.tracker.read_set, result)


class _Graph:
    """One invocation-local tracked graph inspection, never an authority cache."""
    def __init__(self, authority, reader):
        self.authority, self.reader = authority, reader
        self.tracker = ReadTracker(reader)
        self.view = self.tracker.view
        self.headers = {}
        self.work = 0
        self.enforce_budget = True
        self.identities = {}
        self.anchors = {}

    def __enter__(self):
        self.tracker.__enter__()
        return self

    def __exit__(self, *error):
        self.tracker.__exit__(*error)

    def spend(self, amount=1):
        self.work += amount
        if self.enforce_budget and self.work > self.authority.limits.max_work:
            _fail("grant_check_quota")

    def birth(self, ref):
        self.spend()
        self.view.exists(ref)
        return self.reader.existence_version(ref)

    def grant(self, ref):
        if type(ref) is not Ref or ref.key.entity_type != self.authority.grant_type:
            _fail("invalid_grant_reference")
        if ref in self.headers:
            return self.headers[ref]
        self.spend()
        try:
            record = self.view.resolve(ref)
        except EntityUnavailable:
            _fail("missing_grant")
        values = record.values
        task, generation, activity = values["task"], values["task_generation"], values["owner_activity"]
        if ((task is None) != (generation is None) or ((task is not None) == (activity is not None))):
            _fail("grant_owner_xor")
        if task is not None:
            if (type(task) is not Ref or task.key.entity_type != self.authority.task_type
                    or type(generation) is not int or generation < 1):
                _fail("invalid_task_owner")
        else:
            identity = self.authority.identity(activity, entity_only=True)
            if identity[0] not in self.authority.activities:
                _fail("unknown_activity_owner")
        if values["kind"] == "authority" and task is None:
            _fail("authority_requires_task_owner")
        if values["kind"] == "activity" and activity is None:
            _fail("activity_requires_activity_owner")
        if type(values["revoked"]) is not bool or type(values["allow_activity_transfer"]) is not bool:
            _fail("invalid_grant_boolean")
        if type(values["principal"]) is not str or not values["principal"]:
            _fail("invalid_grant_principal")
        for name in ("resources", "created_resources"):
            self.resource_ids(values[name], entity_only=name == "created_resources")
        for name in ("actions", "create_types"):
            items = values[name]
            if (type(items) is not tuple or len(items) > self.authority.limits.max_resources
                    or any(type(x) is not str or not x for x in items) or len(set(items)) != len(items)):
                _fail("invalid_grant_scope")
        if any(typ not in self.authority.descriptors for typ in values["create_types"]):
            _fail("unknown_creation_type")
        if values["parent"] is not None:
            self.authority.identity(values["parent"], grant_only=True)
        elif values["kind"] != "authority":
            _fail("delegated_grant_requires_parent")
        self.headers[ref] = values
        return values

    def original_activity(self, activity, anchor, invocation, operation):
        identity = self.authority.identity(activity, entity_only=True)
        spec = self.authority.activities.get(identity[0])
        if spec is None or not spec[3] or type(activity) is not Ref:
            _fail("invalid_original_activity")
        if (type(invocation) is not Ref or invocation.key.entity_type != self.authority.invocation_type
                or type(anchor) is not Ref or type(operation) is not Ref):
            _fail("invalid_original_start_proof")
        header, op = self.grant(anchor), self.grant(operation)
        if (header["kind"] != "activity" or op["kind"] != "operation"
                or self.authority.identity(header["owner_activity"], entity_only=True) != identity
                or self.authority.identity(op["owner_activity"], entity_only=True) != identity
                or op["parent"] is None or self.authority.identity(op["parent"], grant_only=True)
                    != self.authority.identity(anchor, grant_only=True)
                or op["source_invocation"] is None
                or self.authority.identity(op["source_invocation"], entity_only=True)
                    != self.authority.identity(invocation, entity_only=True)
                or identity not in self.resource_ids(op["created_resources"], entity_only=True)):
            _fail("original_start_graph_mismatch")
        try:
            inv = self.view.resolve(invocation).values
        except EntityUnavailable:
            _fail("missing_original_start_invocation")
        if (inv["grant"] != operation or inv["principal"] != op["principal"]
                or inv["plan_id"] != op["plan_id"] or header["plan_id"] != op["plan_id"]):
            _fail("original_start_invocation_mismatch")
        if (any(ref.key.generation != 1 for ref in (anchor, invocation, operation))
                or invocation.key.entity_id != op["plan_id"]
                or operation.key.entity_id != op["plan_id"] + "-grant"
                or anchor.key.entity_id != op["plan_id"] + "-anchor"):
            _fail("noncanonical_original_start_identity")
        birth = self.birth(operation)
        if not birth or self.birth(anchor) != birth or self.birth(invocation) != birth:
            _fail("original_start_birth_mismatch")
        for ref in self.chain(operation):
            self.integrity(ref)
        self.creation_proof(operation)
        if not self.view.exists(activity):
            if self.birth(activity) <= birth:
                _fail("original_activity_never_created")
            return "absent", spec
        record = self.view.resolve(activity)
        if self.birth(activity) != birth or record.values[spec[2]] != anchor:
            _fail("original_activity_backlink_or_birth_mismatch")
        return ("terminal" if record.values[spec[0]] in spec[3] else "nonterminal"), spec

    def resource_ids(self, values, *, entity_only=False):
        if type(values) is not tuple or len(values) > self.authority.limits.max_resources:
            _fail("invalid_resource_scope")
        cache_key = (id(values), entity_only)
        if cache_key in self.identities:
            return self.identities[cache_key]
        self.spend(len(values))
        result = tuple(self.authority.identity(item, entity_only=entity_only) for item in values)
        if len(set(result)) != len(result):
            _fail("duplicate_resource_scope")
        self.identities[cache_key] = result
        return result

    def chain(self, ref):
        output, seen = [], set()
        while ref is not None:
            if ref in seen or len(output) >= self.authority.limits.max_chain:
                _fail("grant_parent_cycle_or_depth")
            seen.add(ref)
            output.append(ref)
            values = self.grant(ref)
            ref = None if values["parent"] is None else _ref(
                self.authority.identity(values["parent"], grant_only=True))
        return tuple(output)

    def owner(self, ref):
        values = self.grant(ref)
        if values["task"] is not None:
            return ("task", values["task"], values["task_generation"])
        return ("activity", self.authority.identity(values["owner_activity"], entity_only=True))

    def anchor(self, ref):
        if ref in self.anchors:
            return self.anchors[ref]
        values = self.grant(ref)
        if values["owner_activity"] is None:
            return None
        owner = self.authority.identity(values["owner_activity"], entity_only=True)
        cursor, path, seen = ref, [], set()
        while cursor is not None:
            if cursor in seen or len(path) >= self.authority.limits.max_chain:
                _fail("grant_parent_cycle_or_depth")
            seen.add(cursor)
            header = self.grant(cursor)
            if header["kind"] == "activity" and self.authority.identity(
                    header["owner_activity"], entity_only=True) == owner:
                anchor = cursor
                break
            inherited = self.anchors.get(cursor)
            if inherited is not None and self.authority.identity(
                    self.grant(inherited)["owner_activity"], entity_only=True) == owner:
                anchor = inherited
                break
            path.append(cursor)
            cursor = None if header["parent"] is None else _ref(
                self.authority.identity(header["parent"], grant_only=True))
        else:
            _fail("activity_owner_requires_lineage_anchor")
        self.anchors[anchor] = anchor
        for child in path:
            self.anchors[child] = anchor
        self.anchors[ref] = anchor
        return anchor

    def creation_proof(self, ref, *, require_live=False):
        values = self.grant(ref)
        created = self.resource_ids(values["created_resources"], entity_only=True)
        source = values["source_invocation"]
        if not created and source is None:
            return
        if values["kind"] != "operation" or source is None or values["plan_id"] is None:
            _fail("created_resource_requires_operation_proof")
        identity = self.authority.identity(source, entity_only=True)
        if identity[0] != self.authority.invocation_type:
            _fail("invalid_creation_invocation")
        try:
            invocation = self.view.resolve(_ref(identity))
        except EntityUnavailable:
            _fail("missing_creation_invocation")
        birth = self.birth(ref)
        if (self.birth(_ref(identity)) != birth or invocation.values["grant"] != ref
                or invocation.values["plan_id"] != values["plan_id"]
                or invocation.values["principal"] != values["principal"]):
            _fail("creation_invocation_backlink_mismatch")
        scope = self.resource_ids(values["resources"])
        for identity in created:
            target = _ref(identity)
            stamp = self.birth(target)
            if identity not in scope:
                _fail("creation_proof_outside_grant")
            if self.view.exists(target):
                if stamp != birth:
                    _fail("resource_not_created_by_accepted_operation")
            elif require_live or stamp <= birth:
                _fail("invalid_created_resource_history")

    def integrity(self, ref):
        values = self.grant(ref)
        # A known deleted incarnation is historical/inactive. A never-issued
        # future incarnation is not a dormant permission that may activate later.
        for identity in self.resource_ids(values["resources"]):
            if self.birth(_ref(identity)) == 0:
                _fail("resource_identity_never_existed")
        self.creation_proof(ref)
        owner = self.owner(ref)
        owner_ref = owner[1] if owner[0] == "task" else _ref(owner[1])
        if self.view.exists(owner_ref):
            record = self.view.resolve(owner_ref)
            if owner[0] == "task":
                if record.values["generation"] < owner[2] or record.values["principal"] != values["principal"]:
                    _fail("task_owner_identity_mismatch")
            else:
                anchor = self.anchor(ref)
                header = self.grant(anchor)
                spec = self.authority.activities[owner[1][0]]
                if not header["revoked"] and record.values[spec[2]] != anchor:
                    _fail("activity_anchor_backlink_mismatch")
                if record.values[spec[0]] in spec[3] and not header["revoked"]:
                    _fail("activity_terminal_requires_revocation")
        elif self.birth(owner_ref) <= self.birth(ref):
            _fail("owner_never_existed_or_predates_grant")
        if values["parent"] is None:
            return
        parent_ref = _ref(self.authority.identity(values["parent"], grant_only=True))
        parent = self.grant(parent_ref)
        if values["principal"] != parent["principal"]:
            _fail("grant_principal_widened")
        if (not set(values["actions"]).issubset(parent["actions"])
                or not set(values["create_types"]).issubset(parent["create_types"])
                or values["allow_activity_transfer"] and not parent["allow_activity_transfer"]):
            _fail("grant_scope_widened")
        parent_resources = set(self.resource_ids(parent["resources"]))
        created = set(self.resource_ids(values["created_resources"], entity_only=True))
        for identity in self.resource_ids(values["resources"]):
            if identity not in parent_resources and not (
                    identity in created and identity[0] in parent["create_types"]):
                _fail("grant_resource_scope_widened")
        parent_owner = self.owner(parent_ref)
        if owner != parent_owner:
            if (values["kind"] != "activity" or owner[0] != "activity"
                    or not parent["allow_activity_transfer"]):
                _fail("owner_transfer_not_permitted")

    def resource_live(self, identity):
        owner = _ref(identity)
        self.spend()
        if not self.view.exists(owner):
            return False
        if identity[3] is None:
            return True
        members = self.view.field(owner, identity[3])
        if type(members) is not tuple or len(members) > self.authority.limits.max_resources:
            _fail("collection_check_quota")
        self.spend(len(members))
        try:
            self.view.resolve_collection(CollectionRef(owner.key, identity[3])).members
            return True
        except EntityUnavailable:
            return False

    def owner_live(self, ref, *, strict_task=False):
        values = self.grant(ref)
        owner = self.owner(ref)
        owner_ref = owner[1] if owner[0] == "task" else _ref(owner[1])
        if not self.view.exists(owner_ref):
            return False, "owner_unavailable"
        record = self.view.resolve(owner_ref)
        if owner[0] == "task":
            allowed_states = ("active",) if strict_task or values["kind"] != "operation" else ("active", "completed")
            if record.values["generation"] != owner[2] or record.values["state"] not in allowed_states:
                return False, "task_owner_inactive"
        else:
            anchor = self.anchor(ref)
            spec = self.authority.activities[owner[1][0]]
            if (record.values[spec[0]] not in spec[1] or record.values[spec[2]] != anchor):
                return False, "activity_owner_inactive"
        return True, None

    def live(self, ref, *, strict_task=False):
        chain = self.chain(ref)
        for ancestor in chain:
            self.integrity(ancestor)
            if self.grant(ancestor)["revoked"]:
                return False, "grant_revoked"
        # Ancestor OWNER LIFETIMES are intentionally not re-applied. Creation
        # checked live parent; lawful transfer has its own immutable leaf owner.
        return self.owner_live(ref, strict_task=strict_task)

    def validate(self):
        # Startup checks the accepted envelope's entire finite history, not a
        # private active-use quota. Otherwise legal sequential commits could
        # manufacture a graph that rejects itself at the next restart.
        self.enforce_budget = False
        validated, depths = set(), {}
        for start in self.view.entities(self.authority.grant_type):
            path, seen = [], set()
            cursor = start
            while cursor is not None and cursor not in validated:
                if cursor in seen:
                    _fail("grant_parent_cycle_or_depth")
                seen.add(cursor)
                path.append(cursor)
                values = self.grant(cursor)
                cursor = None if values["parent"] is None else _ref(
                    self.authority.identity(values["parent"], grant_only=True))
            for ref in reversed(path):
                values = self.grant(ref)
                parent = None if values["parent"] is None else _ref(
                    self.authority.identity(values["parent"], grant_only=True))
                depth = 1 if parent is None else depths[parent] + 1
                if depth > self.authority.limits.max_chain:
                    _fail("grant_parent_cycle_or_depth")
                self.integrity(ref)
                depths[ref] = depth
                validated.add(ref)
        for typ, spec in self.authority.activities.items():
            for ref in self.view.entities(typ):
                activity = self.view.resolve(ref)
                backlink = activity.values[spec[2]]
                if backlink is None:
                    if activity.values[spec[0]] in spec[1]:
                        _fail("active_activity_requires_anchor")
                    continue
                values = self.grant(backlink)
                if (values["kind"] != "activity"
                        or self.authority.identity(values["owner_activity"], entity_only=True)
                        != self.authority.identity(ref, entity_only=True)):
                    _fail("activity_anchor_backlink_mismatch")
