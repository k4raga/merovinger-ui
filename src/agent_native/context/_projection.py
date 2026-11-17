"""Borrowed snapshot projection for ContextIndex and trusted export consumers.

Decisions/issued traces are immutable snapshot data, NOT authentication, current
export permission or pixel proof. Broker obtains CURRENT guarded authority and
authenticates through C2 separately. No reader/closure is returned by this API.
"""
from dataclasses import dataclass, field as dc_field
from collections.abc import Mapping
from functools import wraps

from agent_native.schema import Ref, CollectionRef
from agent_native.state import deep_freeze, EntityUnavailable, ReadViewUnavailable
from agent_native.state.tracking import ReadTracker, TrackingClosed
from agent_native.kernel._grant_authority import GrantIntegrityError
from .bindings import identity, resolve_view_bindings
from .policy import ContextUnavailable
from .semantic import SemanticProjection, scalar


def _snapshot_method(method):
    @wraps(method)
    def run(self, *args, **kwargs):
        self._check()
        try:
            value = method(self, *args, **kwargs)
            self._check()
            return value
        except (GrantIntegrityError, EntityUnavailable, ReadViewUnavailable, TrackingClosed, KeyError, ValueError) as error:
            raise ContextUnavailable() from error
    return run


@dataclass(frozen=True, slots=True)
class Direct:
    pass


@dataclass(frozen=True, slots=True)
class Activity:
    activity: Ref

    def __post_init__(self):
        if type(self.activity) is not Ref:
            raise TypeError("Activity route needs an exact Ref")


@dataclass(frozen=True, slots=True)
class Member:
    alias: CollectionRef
    via: Direct | Activity = dc_field(default_factory=Direct)

    def __post_init__(self):
        if type(self.alias) is not CollectionRef or type(self.via) not in (Direct, Activity):
            raise TypeError("Use an original alias and Direct/Activity parent")


@dataclass(frozen=True, slots=True)
class Selector:
    kind: str
    target: Ref | CollectionRef | str
    fields: tuple[str, ...] | None = None
    via: Direct | Activity | Member = dc_field(default_factory=Direct)

    def __post_init__(self):
        expected = {"entity": Ref, "field": Ref, "collection": CollectionRef, "command": str}
        if (self.kind not in expected or type(self.target) is not expected[self.kind]
                or type(self.via) not in (Direct, Activity, Member)
                or (self.fields is not None and (type(self.fields) is not tuple
                    or any(type(v) is not str or not v for v in self.fields)))
                or (self.kind == "field" and (self.fields is None or len(self.fields) != 1))):
            raise TypeError("Use a closed typed inspect selector")


@dataclass(frozen=True, slots=True)
class _Access:
    source: Ref | CollectionRef
    route: Direct | Activity | Member
    anchor: Ref | None
    chain: tuple[Ref, ...]


@dataclass(frozen=True, slots=True)
class _Sample:
    kind: str
    source: Ref | CollectionRef | str
    name: str | None
    route: Direct | Activity | Member
    value: object
    view: Ref | None = None
    binding: Ref | None = None
    draft: Ref | None = None


@dataclass(frozen=True, slots=True, weakref_slot=True)
class ProjectionTrace:
    original_cut: object
    schema: tuple
    request_key: tuple
    accesses: tuple[_Access, ...]
    samples: tuple[_Sample, ...]
    sampled_value: object
    read_set: object
    _issuer: object = dc_field(repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class SnapshotDecision:
    allowed: bool
    value: object
    read_set: object
    trace: ProjectionTrace | None = None
    authority_cut: object = None
    request_key: tuple = ()


class SnapshotProjector:
    """One borrowed managed snapshot; methods after exit/expiry fail closed."""
    def __init__(self, index, cut, request):
        self._index, self._cut = index, cut
        self._topology = index.require_ready(cut)
        self._policy = index.policy(cut, request)
        self._reads = ReadTracker(cut.view)
        self._reads.include(self._policy.read_set)
        for key in index.required_keys:
            self._reads.depend_on_query(key, cut.derived_entries[key].version)
            self._reads.include(cut.derived_entries[key].read_set)
        self._view = self._reads.view
        self._closed = False
        self._accesses, self._samples = [], []
        self._activity = {}
        self.scope = frozenset(identity(v) for v in self._policy.resources)
        direct = index.authority.check_use(cut.view, request.grant, principal=request.actor)
        self._reads.include(direct.read_set)
        self._direct_chain = direct.chain
        self._semantic = SemanticProjection(request.rules, index.declarations, index.private_types,
            self.literal_source, identity_record_type=index.system.ResourceIdentity.__record_type__)

    def __enter__(self):
        self._check()
        return self

    def __exit__(self, *error):
        self.close()

    def close(self):
        if not self._closed:
            self._closed = True
            self._reads.close()
            self._view = None
            self._reads = None
            self._cut = None
            self._semantic = None
            self._topology = None
            self._samples.clear()
            self._accesses.clear()
            self._activity.clear()
            self.scope = frozenset()
            self._policy = None

    def _check(self):
        if self._closed:
            raise ContextUnavailable()
        try:
            self._reads._check()
            if not self._reads.read_set.complete:
                raise ContextUnavailable()
        except (ReadViewUnavailable, TrackingClosed) as error:
            raise ContextUnavailable() from error

    @staticmethod
    def scoped(source, available):
        return source in available or (type(source) is CollectionRef and Ref(source.owner) in available)

    def permit(self, source, name=None):
        self._check()
        key = source.key if type(source) is Ref else source.owner
        descriptor = self._index.entities.get(key.entity_type)
        if descriptor is None or key.entity_type in self._index.private_types:
            return False
        ref = Ref(key)
        name = source.field if type(source) is CollectionRef else name
        return (self._policy.request.rules.permits(descriptor, ref) and self._view.exists(ref)
                and (name is None or self._policy.request.rules.permits(descriptor, ref, name)))

    def activity(self, ref):
        """Canonical anchor/use scope, shared with ContextIndex's Activity roots."""
        self._check()
        if ref in self._activity:
            return self._activity[ref]
        spec = self._index.authority.activities.get(ref.key.entity_type)
        result = None
        if spec is not None and self.permit(ref):
            grant = self._view.field(ref, spec[2])
            if grant is not None:
                use = self._index.authority.check_use(self._cut.view, grant, principal=self._policy.request.actor)
                self._reads.include(use.read_set)
                if use.allowed and self._view.field(grant, "kind") == "activity":
                    result = (grant, use.chain, frozenset(identity(v) for v in use.resources))
        self._activity[ref] = result
        return result

    def _scope(self, route):
        if type(route) is Direct:
            return (None, self._direct_chain, self.scope)
        if type(route) is Activity:
            return self.activity(route.activity)
        return None

    def authorize(self, source, route=Direct(), *, record=True):
        self._check()
        try:
            source = self._index._source(source)
        except (TypeError, KeyError, ValueError):
            return None
        if not self.permit(source):
            return None
        if type(route) is Member:
            if type(source) is not Ref:
                return None
            parent = self.authorize(route.alias, route.via, record=record)
            if parent is None or source not in self._view.resolve_collection(route.alias).members:
                return None
            access = _Access(source, route, parent.anchor, parent.chain)
        elif type(route) in (Direct, Activity):
            scope = self._scope(route)
            if scope is None:
                return None
            anchor, chain, available = scope
            owner = type(route) is Activity and source == route.activity
            if not owner and not self.scoped(source, available):
                return None
            access = _Access(source, route, anchor, chain)
        else:
            return None
        if record and access not in self._accesses:
            self._accesses.append(access)
        return access

    def route_for(self, source):
        """Resolve only original direct sources/Owned membership; never rank data."""
        self._check()
        routes = [Direct()]
        routes.extend(Activity(row[0]) for row in self._topology["activities"])
        for route in routes:
            if self.authorize(source, route) is not None:
                return route
        if type(source) is Ref and self.permit(source):
            for route in routes:
                scope = self._scope(route)
                if scope is None:
                    continue
                for owner in sorted(scope[2], key=repr):
                    if type(owner) is CollectionRef:
                        aliases = (owner,)
                    else:
                        aliases = tuple(CollectionRef(owner.key, item.name)
                            for item in self._index.entities[owner.key.entity_type].fields
                            if item.relation is not None and item.relation.kind == "owned"
                            and not item.relation.nullable)
                    for alias in aliases:
                        via = Member(alias, route)
                        if self.authorize(source, via) is not None:
                            return via
        return None

    @_snapshot_method
    def extension(self, activation):
        """Authorize one SAME installed descriptor/output revision and mint trace."""
        kernel = self._index._extension_kernel
        if kernel is None or not self._policy.allowed:
            return self._decision(False, trace=False)
        try:
            from agent_native.extensions.views import snapshot
            value = snapshot(kernel, self._cut, activation,
                principal=self._policy.request.actor, request=self._policy.request, reads=self._reads)
        except (TypeError, ValueError, LookupError):
            return self._decision(False, trace=False)
        self._samples.append(_Sample("extension", activation, None, Direct(), value))
        return self._decision(True, value)

    def literal_source(self, source):
        self._check()
        return self.route_for(source) is not None

    def _annotation(self, ref, name):
        definition = self._index.declarations.get(ref.key.entity_type)
        if definition is None or not self.permit(ref, name):
            return None
        annotation = dict(definition.annotations).get(name)
        return None if annotation is None else (definition, annotation)

    def field_value(self, ref, name, *, purpose="value", sampled=None, historical=False):
        """The SAME declaration/value predicate used by the existing serializer."""
        self._check()
        if type(ref) is not Ref or purpose not in ("value", "text", "provenance"):
            return False, None
        known = self._annotation(ref, name)
        if known is None:
            return False, None
        definition, annotation = known
        if (purpose == "provenance" and not scalar(annotation)) or not self._semantic.field(
                definition, name, text=purpose == "text"):
            return False, None
        value = sampled if historical else self._view.field(ref, name)
        if not self._semantic.value(annotation, value):
            return False, None
        return True, value

    def _mint(self, value):
        self._check()
        trace = ProjectionTrace(self._cut.identity, self._cut.identity.schema_manifest,
            self._request_key(), tuple(self._accesses), tuple(self._samples),
            deep_freeze(value), self._reads.read_set, self._index._issuer)
        self._index._projection_traces[id(trace)] = trace
        return trace

    def _request_key(self):
        request = self._policy.request
        return (request.actor, request.task, self._policy.task_generation, request.grant, request.destination)

    def _decision(self, allowed, value=None, *, trace=True):
        self._check()
        sampled = deep_freeze(value) if allowed else None
        issued = self._mint(sampled) if allowed and trace else None
        return SnapshotDecision(allowed, sampled, self._reads.read_set, issued,
                                self._cut.identity, self._request_key())

    @_snapshot_method
    def field(self, ref, name, *, via=Direct(), purpose="value"):
        self._check()
        if not self._policy.allowed or self.authorize(ref, via) is None:
            return self._decision(False)
        allowed, value = self.field_value(ref, name, purpose=purpose)
        if allowed:
            self._samples.append(_Sample("field", ref, name, via, deep_freeze(value)))
        return self._decision(allowed, value)

    def _draft_binding(self, draft, view, route, *, expected=None, historical=False):
        if (type(draft) is not Ref or draft.key.entity_type != self._index.ui.Draft.__entity_type__
                or type(view) is not Ref or view.key.entity_type != self._index.ui.EngineView.__entity_type__):
            return None
        # Fresh UI facts belong to the CURRENT request's canonical view.
        # Historical issued samples retain their original association separately.
        if not historical and self._view.field(self._policy.request.focus, "view") != view:
            return None
        if self._cut.view.resolve(draft).partition != "memory":
            return None
        binding = self._view.field(draft, "binding")
        page = resolve_view_bindings(self._view, view, self._index.sources)
        if page.principal != self._policy.request.actor or page.state != "open":
            return None
        row = next((r for r in page.rows if r.binding == binding and r.effective_visible), None)
        if row is None or type(row.source.source) is not Ref:
            return None
        source, name = row.source.source, row.source.field
        if expected is not None and (source, name, binding) != expected:
            return None
        if self.authorize(source, route) is None or not self._semantic.field(
                self._index.declarations[source.key.entity_type], name, text=True):
            return None
        return source, name, binding

    @_snapshot_method
    def draft(self, draft_ref, view_ref, *, via=Direct()):
        self._check()
        binding = self._draft_binding(draft_ref, view_ref, via) if self._policy.allowed else None
        if binding is None:
            return self._decision(False)
        source, name, ref = binding
        value = deep_freeze({"text": self._view.field(draft_ref, "text"),
                             "preedit": self._view.field(draft_ref, "preedit")})
        self._samples.append(_Sample("draft", source, name, via, value, view_ref, ref, draft_ref))
        return self._decision(True, value)

    @_snapshot_method
    def membership(self, alias, *, via=Direct()):
        """Identity/geometry dependency only: hidden members deny the whole proof."""
        self._check()
        if (not self._policy.allowed or type(alias) is not CollectionRef
                or type(via) not in (Direct, Activity) or self.authorize(alias, via) is None):
            return self._decision(False)
        members = self._view.resolve_collection(alias).members
        if any(self.authorize(member, Member(alias, via)) is None for member in members):
            return self._decision(False)
        self._samples.append(_Sample("membership", alias, None, via, members))
        return self._decision(True, members)

    @_snapshot_method
    def inspect(self, selector):
        self._check()
        if type(selector) is not Selector:
            raise TypeError("Use a closed Selector")
        return self._index._plan(self._cut, self._topology, self._policy,
                                 projector=self, selectors=(selector,), include_roots=False)

    def record_field(self, ref, name, value, *, relation=False):
        route = self.route_for(ref)
        if route is None:
            return False
        known = self._annotation(ref, name)
        if known is None or not self._semantic.field(known[0], name):
            return False
        if not relation and not self.field_value(ref, name, sampled=value, historical=True)[0]:
            return False
        self._samples.append(_Sample("relation" if relation else "field", ref, name, route, deep_freeze(value)))
        return True

    def record_capability(self, name, *, metadata=False):
        definition = self._index.declarations[name]
        if not self._policy.request.rules.permits(definition.descriptor, metadata=metadata):
            return False
        self._samples.append(_Sample("capability", name, None, Direct(), metadata))
        return True

    def record_command(self, name):
        definition = self._index.declarations[name]
        if not self._semantic.definition(definition):
            return False
        self._samples.append(_Sample("command", name, None, Direct(), None))
        return True

    def selector_field(self, ref, name, *, via):
        """Seal the field-eligibility reason BEFORE relevance/count truncation.

        The sampled value is private proof data, not an emitted body. Recheck
        applies current policy to this original value, never current substitution.
        Every eligible reason is retained conservatively for a field subset.
        """
        if self.authorize(ref, via) is None:
            return False
        allowed, value = self.field_value(ref, name)
        if allowed:
            self._samples.append(_Sample("eligibility", ref, name, via, deep_freeze(value)))
        return allowed

    def selector_seeds(self, selectors):
        seeds, commands = [], set()
        for selector in selectors:
            if type(selector) is not Selector:
                raise TypeError("Use closed typed selectors")
            if not self._policy.allowed:
                continue
            if selector.kind == "command":
                pair = self._index.commands.get(selector.target)
                if pair is not None and self._policy.request.rules.permits(pair[1]):
                    commands.add(selector.target)
            elif self.authorize(selector.target, selector.via) is not None:
                if selector.kind == "field":
                    if self.selector_field(selector.target, selector.fields[0], via=selector.via):
                        seeds.append((-1, selector.target, selector.fields[0]))
                elif selector.fields is None:
                    seeds.append((-1, selector.target, None))
                elif selector.kind == "collection":
                    owner = self._index.entities[selector.target.owner.entity_type]
                    relation = next(f.relation for f in owner.fields if f.name == selector.target.field)
                    definition = self._index.declarations[relation.target_type]
                    for name in selector.fields:
                        if self._semantic.field(definition, name):
                            seeds.append((-1, selector.target, name))
                else:
                    for name in selector.fields:
                        if type(selector.target) is Ref and self.selector_field(selector.target, name, via=selector.via):
                            seeds.append((-1, selector.target, name))
        return seeds, commands

    @_snapshot_method
    def recheck(self, trace):
        """Reauthorize original values in the supplied snapshot, exact destination.

        Broker MUST supply CURRENT guarded authority. This method is not auth.
        """
        return self._recheck(trace, retarget=False)

    @_snapshot_method
    def retarget(self, trace):
        """Explicit destination-only reauthorization; ORIGINAL trace stays intact.

        Broker authenticates through C2 and mints a NEW destination-bound handle.
        Cross-task/grant adoption and direct reuse of an old handle are unsupported.
        """
        return self._recheck(trace, retarget=True)

    def _recheck(self, trace, *, retarget):
        self._check()
        current_key = self._request_key()
        if (type(trace) is not ProjectionTrace or trace._issuer is not self._index._issuer
                or self._index._projection_traces.get(id(trace)) is not trace
                or trace.schema != self._cut.identity.schema_manifest
                or trace.original_cut.epoch != self._cut.identity.epoch
                or (trace.request_key[:4] != current_key[:4] if retarget else trace.request_key != current_key)
                or not self._policy.allowed):
            return self._decision(False, trace=False)
        for old in trace.accesses:
            current = self.authorize(old.source, old.route)
            if current is None or (current.anchor, current.chain) != (old.anchor, old.chain):
                return self._decision(False, trace=False)
        for sample in trace.samples:
            if sample.kind == "extension":
                from agent_native.extensions.views import snapshot
                from agent_native.state import strict_equal
                try:
                    value = snapshot(self._index._extension_kernel, self._cut, sample.source,
                        principal=self._policy.request.actor, request=self._policy.request, reads=self._reads)
                except (TypeError, ValueError, LookupError):
                    return self._decision(False, trace=False)
                if not strict_equal(value, sample.value):
                    return self._decision(False, trace=False)
            elif sample.kind in ("field", "eligibility"):
                if not self.field_value(sample.source, sample.name, sampled=sample.value, historical=True)[0]:
                    return self._decision(False, trace=False)
            elif sample.kind == "draft":
                if self._draft_binding(sample.draft, sample.view, sample.route,
                        expected=(sample.source, sample.name, sample.binding), historical=True) is None:
                    return self._decision(False, trace=False)
                if any(type(v) is not str for v in sample.value.values()):
                    return self._decision(False, trace=False)
            elif sample.kind == "membership":
                members = self._view.resolve_collection(sample.source).members
                if any(old not in members or self.authorize(old, Member(sample.source, sample.route)) is None
                       for old in sample.value):
                    return self._decision(False, trace=False)
            elif sample.kind == "relation":
                known = self._annotation(sample.source, sample.name)
                if known is None or not self._semantic.field(known[0], sample.name):
                    return self._decision(False, trace=False)
            elif sample.kind == "capability":
                definition = self._index.declarations.get(sample.source)
                if definition is None or not self._policy.request.rules.permits(
                        definition.descriptor, metadata=sample.value):
                    return self._decision(False, trace=False)
                if sample.value and not self._semantic.definition(definition):
                    return self._decision(False, trace=False)
            elif sample.kind == "command":
                definition = self._index.declarations.get(sample.source)
                if definition is None or not self._policy.request.rules.schema(definition.descriptor) or not self._semantic.definition(definition):
                    return self._decision(False, trace=False)
            else:
                return self._decision(False, trace=False)
        self._check()
        return SnapshotDecision(True, trace.sampled_value, self._reads.read_set, trace,
                                self._cut.identity, current_key)
