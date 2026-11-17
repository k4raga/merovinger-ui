"""One rebuildable lightweight C1 root query and policy-first detailed projection."""
from collections.abc import Mapping
from types import MappingProxyType
from weakref import WeakValueDictionary

from agent_native.schema import Ref, EntityKey, CollectionRef
from agent_native.state import deep_freeze, EntityUnavailable, ReadViewUnavailable
from agent_native.state.query import QueryKey
from agent_native.state.derived import QueryDefinition
from agent_native.state.tracking import ReadTracker
from agent_native.kernel.coordinator import CutIdentity
from agent_native.kernel._grant_authority import GrantAuthority, GrantIntegrityError
from .definitions import ContextDefinitions
from .policy import ContextRequest, PolicyView, ContextPlan, ContextUnavailable
from .semantic import SemanticProjection, scalar, base


from .bindings import identity, resource_record, BindingSources, resolve_view_bindings


def _key(value):
    return value.key if type(value) is Ref else value.owner


class ContextIndex:
    """Engine-owned pure producer over the existing DerivedCache.

    No store, subscriptions, app callbacks, network or mutable context authority.
    Construct after ALL declarations. SDK bootstrap installs query_definitions in
    DerivedCache and passes the SAME required_keys tuple to Coordinator and gate.
    """
    def __init__(self, system, ui: ContextDefinitions, registries):
        if type(ui) is not ContextDefinitions:
            raise TypeError("Install canonical context definitions first")
        self.system, self.ui, self.registries = system, ui, tuple(registries)
        self.authority = GrantAuthority(system, self.registries)
        self.entities = self.authority.descriptors
        self.owners = MappingProxyType({d.definition_id: r for r in self.registries
                                       for d in r.descriptors(kind="entity")})
        self.commands = MappingProxyType({d.definition_id: (r, d) for r in self.registries
                                         for d in r.descriptors(kind="command")})
        # Reuse compiled original annotations; never run their validators/codecs.
        self.declarations = MappingProxyType({d.definition_id: r._definition(d.kind, d.definition_id)
                                              for r in self.registries for d in r.descriptors()})
        self.private_types = frozenset(d.definition_id for d in system.registry.descriptors(kind="entity"))
        self.sources = BindingSources(self.registries, self.private_types, binding_type=ui.Binding.__entity_type__)
        self.root_key = QueryKey(system.registry.namespace + ".context_roots")
        self.required_keys = (self.root_key,)
        self.query_definitions = (QueryDefinition(self.root_key, self._roots),)
        self._extension_kernel = None
        self._issuer = object()
        self._projection_traces = WeakValueDictionary()

    def bind_extensions(self, kernel):
        """Bind the SAME trusted kernel; no app callback/catalogue is accepted."""
        if not self.system.supports_extensions:
            raise ValueError("Extensions are unavailable in the UI profile")
        if tuple(kernel.registries) != self.registries or kernel.system is not self.system:
            raise ValueError("Extensions require SAME Registry/kernel")
        if self._extension_kernel is not None and self._extension_kernel is not kernel:
            raise ValueError("Extension kernel already bound")
        self._extension_kernel = kernel

    def _source(self, value):
        return self.sources.source(value)

    def resolve_binding(self, values):
        return self.sources.resolve(values)

    @staticmethod
    def _memory(view, ref):
        record = view.resolve(ref)
        if record.partition != "memory":
            raise ValueError("Semantic UI records must be memory-only")
        return record.values

    def _roots(self, view):
        focuses = []
        for ref in view.entities(self.ui.ContextFocus.__entity_type__, partition="memory"):
            focus = self._memory(view, ref)
            bindings, selection, draft = (), (), None
            if focus["view"] is not None:
                page = resolve_view_bindings(view, focus["view"], self.sources)
                if page.principal != focus["principal"]:
                    raise ValueError("View principal mismatch")
                if page.state == "open":
                    rows = []
                    for row in page.rows:
                        if not row.effective_visible:
                            continue
                        values = row.source
                        if values.role == "command" and values.command_id not in self.commands:
                            raise ValueError("Unknown bound command")
                        rows.append((row.binding, values.path, values.role, values.source,
                                     values.field, values.command_id))
                    bindings = tuple(rows)
                    all_known = {row.binding for row in page.rows}
                    known = {row[0] for row in bindings}
                    if focus["selection"] is not None:
                        selected = self._memory(view, focus["selection"])
                        if selected["binding"] not in all_known:
                            raise ValueError("Selection belongs to another view")
                        if selected["binding"] in known:
                            selection = (selected["binding"], tuple(self._source(v) for v in selected["resources"]))
                    if focus["draft"] is not None:
                        draft_ref = focus["draft"]
                        if view._tracker._reader.resolve(draft_ref).partition != "memory":
                            raise ValueError("Draft must be memory-only")
                        draft_binding = view.field(draft_ref, "binding")
                        if draft_binding not in all_known:
                            raise ValueError("Draft belongs to another view")
                        # Text/preedit are deliberately NOT read/materialized here.
                        if draft_binding in known:
                            draft = (focus["draft"], draft_binding)
            task_resources = ()
            if focus["task"] is not None:
                task = focus["task"]
                if task.key.entity_type != self.system.TaskSession.__entity_type__:
                    raise ValueError("Invalid focus task")
                if view.field(task, "principal") != focus["principal"]:
                    raise ValueError("Task principal mismatch")
                if view.field(task, "state") == "active":
                    task_resources = tuple(self._source(v) for v in view.field(task, "working_resources"))
            focuses.append((ref, focus["principal"], focus["task"], bindings, selection, draft, task_resources))
        activities = []
        # Typed activity-only scans. Unrelated grants/system rows do not consume
        # matching-ref quota. Existing C1 scans remain O(N), no CPU SLA is claimed.
        for typ, spec in sorted(self.authority.activities.items()):
            for activity in view.entities(typ):
                if view.field(activity, spec[0]) not in spec[1]:
                    continue
                grant = view.field(activity, spec[2])
                if grant is None:  # ordinary inactive/unowned construction state
                    continue
                resources = []
                for field in self.entities[typ].fields:
                    if field.relation is not None and field.relation.kind in ("resource", "resource_collection"):
                        value = view.field(activity, field.name)
                        if value is not None:
                            resources.append(self._source(value))
                activities.append((activity, grant, tuple(resources)))
        return {"focuses": tuple(focuses), "activities": tuple(activities)}

    def require_ready(self, cut):
        if CutIdentity.read(cut.view) != cut.identity:
            raise ContextUnavailable()
        versions = set()
        for key in self.required_keys:
            entry = cut.derived_entries.get(key)
            if (entry is None or entry.status != "current" or not entry.read_set.complete
                    or not entry.priority_evaluated):
                raise ContextUnavailable()
            versions.add(entry.version.cache_incarnation)
            if (not entry.read_set.matches(cut.view, query_versions={
                    k: e.version for k, e in cut.derived_entries.items()})
                    or any(dep.key not in self.required_keys for dep in entry.read_set.queries)):
                raise ContextUnavailable()
        if len(versions) != 1:
            raise ContextUnavailable()
        return cut.query(self.root_key)

    def policy(self, cut, request):
        try:
            return self._policy(cut, request)
        except ContextUnavailable:
            raise
        except (GrantIntegrityError, EntityUnavailable, ReadViewUnavailable, KeyError, ValueError) as error:
            raise ContextUnavailable() from error

    def _policy(self, cut, request):
        if type(request) is not ContextRequest:
            raise TypeError("Use a typed ContextRequest")
        with ReadTracker(cut.view) as reads:
            view = reads.view
            if (request.task.key.entity_type != self.system.TaskSession.__entity_type__
                    or request.grant.key.entity_type != self.system.Grant.__entity_type__
                    or request.focus.key.entity_type != self.ui.ContextFocus.__entity_type__):
                raise ContextUnavailable()
            task = view.resolve(request.task)
            generation = task.values["generation"]
            allowed = (task.values["principal"] == request.actor and task.values["state"] == "active")
            use = self.authority.check_use(cut.view, request.grant, principal=request.actor)
            reads.include(use.read_set)
            grant_task = view.field(request.grant, "task")
            grant_generation = view.field(request.grant, "task_generation")
            allowed = allowed and use.allowed and grant_task == request.task and grant_generation == generation
            view.policy_revision
            # Focus is a canonical C1 binding, not a request-supplied root list.
            focus = self._memory(view, request.focus)
            allowed = allowed and focus["principal"] == request.actor and focus["task"] == request.task
        semantic = SemanticProjection(request.rules, self.declarations, self.private_types, lambda source: False,
                                      identity_record_type=self.system.ResourceIdentity.__record_type__)
        scalar_fields = frozenset((typ, name) for typ, definition in self.declarations.items()
            if definition.descriptor.kind == "entity" and typ not in self.private_types
            for name, annotation in definition.annotations
            if scalar(annotation) and semantic.field(definition, name))
        text_fields = frozenset((typ, name) for typ, definition in self.declarations.items()
            if definition.descriptor.kind == "entity" and typ not in self.private_types
            for name, annotation in definition.annotations
            if base(annotation) is str and semantic.field(definition, name, text=True))
        return PolicyView(request, cut.identity, generation, allowed, use.resources,
                          reads.read_set, self._issuer, self.private_types, scalar_fields, text_fields)

    def projection(self, cut, request):
        """Borrowed same-cut projector; never authentication/current authority."""
        from ._projection import SnapshotProjector
        try:
            return SnapshotProjector(self, cut, request)
        except (GrantIntegrityError, EntityUnavailable, ReadViewUnavailable, KeyError, ValueError) as error:
            raise ContextUnavailable() from error

    def plan(self, cut, request, *, selectors=()):
        """One policy→authorized seeds→rank→serializer path, including inspect."""
        try:
            with self.projection(cut, request) as projector:
                return self._plan(cut, projector._topology, projector._policy,
                                  projector=projector, selectors=selectors)
        except ContextUnavailable:
            raise
        except (GrantIntegrityError, EntityUnavailable, ReadViewUnavailable, KeyError, ValueError) as error:
            raise ContextUnavailable() from error

    def _plan(self, cut, topology, policy, *, projector, selectors=(), include_roots=True):
        projector._check()
        request, rules, budget = policy.request, policy.request.rules, policy.request.budget
        reads, view, scope = projector._reads, projector._view, projector.scope
        permit, scoped, semantic = projector.permit, projector.scoped, projector._semantic
        seeds, draft_sources, activity_scopes = [], [], {}
        if policy.allowed and include_roots:
            focus = next((f for f in topology["focuses"] if f[0] == request.focus), None)
            if focus is not None:
                _, _, _, bindings, selected, draft, working = focus
                binding_sources = {}
                for binding, path, role, source, name, command in bindings:
                    if source is not None and scoped(source, scope) and permit(source, name):
                        seeds.append((0, source, name))
                        binding_sources[binding] = (source, name, path)
                if selected and selected[0] in binding_sources:
                    parent = binding_sources[selected[0]][0]
                    members = set(view.resolve_collection(parent).members) if type(parent) is CollectionRef else set()
                    for source in selected[1]:
                        if (scoped(source, scope) or source in members) and permit(source):
                            seeds.append((1, source, None))
                if draft is not None and draft[1] in binding_sources:
                    draft_sources.append((draft[0], *binding_sources[draft[1]]))
                for source in working:
                    if scoped(source, scope) and permit(source):
                        seeds.append((2, source, None))
        for activity, grant, resources in topology["activities"]:
            use = projector.activity(activity)
            if use is None:
                continue
            available = use[2]
            activity_scopes[activity] = available
            if include_roots:
                seeds.append((3, activity, None))
                for source in resources:
                    if scoped(source, available) and permit(source):
                        seeds.append((3, source, None))
        extra, selected_commands = projector.selector_seeds(selectors)
        seeds.extend(extra)
        roots, candidates = [], {}
        def add(source, priority, depth, names=None, route=None):
            from ._projection import Member
            route = projector.route_for(source) if route is None else route
            if not permit(source) or route is None or projector.authorize(source, route) is None:
                return
            if type(source) is CollectionRef:
                # Filter source-field policy BEFORE membership/counts.
                if not rules.permits(self.entities[source.owner.entity_type], Ref(source.owner), source.field):
                    return
                members = view.resolve_collection(source).members
                for member in members:
                    eligible = names
                    if names is not None:
                        eligible = tuple(name for name in names if projector.selector_field(
                            member, name, via=Member(source, route)))
                        if not eligible:
                            continue
                    add(member, priority, depth, eligible, route=Member(source, route))
                return
            old = candidates.get(source)
            rank = (priority, depth, source.key.entity_type, source.key.entity_id, source.key.generation)
            if old is None:
                candidates[source] = [rank, set() if names is not None else None]
            else:
                old[0] = min(old[0], rank)
                if names is None:
                    old[1] = None
            if names is not None and candidates[source][1] is not None:
                candidates[source][1].update(names)
        for priority, source, name in seeds:
            if permit(source, name):
                # A named binding is a field-derived root just like an inspect
                # selector. Seal its eligibility reason before counts/budget.
                # Unrestricted entity/working roots (name=None) stay independent.
                if type(source) is Ref and name is not None:
                    route = projector.route_for(source)
                    if route is None or not projector.selector_field(source, name, via=route):
                        continue
                if source not in roots:
                    roots.append(source)
                add(source, priority, 0, None if name is None else (name,))
        objects, fields_used, done = [], 0, set()
        while True:
            remaining = [(ref, value) for ref, value in candidates.items() if ref not in done]
            if not remaining:
                break
            ref, (rank, names) = min(remaining, key=lambda row: row[1][0])
            done.add(ref)
            if len(objects) >= budget.max_objects:
                continue
            descriptor = self.entities[ref.key.entity_type]
            fields = {}
            for item in descriptor.fields:
                if names is not None and item.name not in names:
                    continue
                if not rules.permits(descriptor, ref, item.name) or fields_used >= budget.max_fields:
                    continue
                relation = item.relation
                annotation = dict(self.declarations[descriptor.definition_id].annotations)[item.name]
                if relation is None and not semantic.field(self.declarations[descriptor.definition_id], item.name):
                    continue
                value = view.field(ref, item.name)
                if relation is not None:
                    if value is None:
                        fields[item.name] = None
                    elif relation.kind in ("owned", "resource_collection"):
                        alias = (CollectionRef(ref.key, item.name) if relation.kind == "owned" else value)
                        if ref in activity_scopes and not scoped(alias, activity_scopes[ref]):
                            continue
                        if rank[1] < budget.max_depth:
                            add(alias, rank[0], rank[1] + 1)
                        # No raw members or hidden totals are exported.
                        fields[item.name] = {"relation": relation.kind}
                    elif type(value) is Ref and permit(value):
                        if ref in activity_scopes and not scoped(value, activity_scopes[ref]):
                            continue
                        fields[item.name] = value
                        if rank[1] < budget.max_depth:
                            add(value, rank[0], rank[1] + 1,
                                None if relation.kind == "resource" else ())
                    # denied/private target contributes nothing
                elif semantic.value(annotation, value):
                    fields[item.name] = value
                if item.name in fields:
                    if projector.record_field(ref, item.name, fields[item.name],
                            relation=item.relation is not None and isinstance(fields[item.name], Mapping)):
                        fields_used += 1
                    else:
                        del fields[item.name]
            objects.append({"ref": ref, "fields": fields})
        included = {row["ref"] for row in objects}
        roots = [root for root in roots if (
            root in included if type(root) is Ref else
            any(member in included for member in view.resolve_collection(root).members))]
        drafts = []
        for draft, source, name, path in draft_sources:
            if type(source) is Ref and source in included:
                route = projector.route_for(source)
                focus = next((f for f in topology["focuses"] if f[0] == request.focus), None)
                page = view.field(request.focus, "view") if focus is not None else None
                if route is not None and page is not None:
                    decision = projector.draft(draft, page, via=route)
                    if decision.allowed:
                        drafts.append({"path": path, **dict(decision.value)})
        manifest, schemas = [], []
        for command, (registry, descriptor) in sorted(self.commands.items()):
            if not include_roots and command not in selected_commands:
                continue
            if not rules.permits(descriptor):
                continue
            entry = {"id": command}
            schema_safe = rules.schema(descriptor) and semantic.definition(self.declarations[command])
            if schema_safe and rules.permits(descriptor, metadata=True):
                entry["description"] = descriptor.metadata.get("description", "")
            if projector.record_capability(command, metadata="description" in entry):
                manifest.append(entry)
            if schema_safe and projector.record_command(command):
                schemas.append(registry.tool_schema(command))
        extensions = []
        if policy.allowed and self._extension_kernel is not None:
            for activation in view.entities(self.system.ExtensionActivation.__entity_type__):
                decision = projector.extension(activation)
                if decision.allowed:
                    extensions.append(decision.value)
        payload = deep_freeze({"objects": tuple(objects), "drafts": tuple(drafts),
            "capabilities": tuple(manifest), "schemas": tuple(schemas),
            **({"extensions": tuple(extensions)} if extensions else {}),
            "coverage": {"authorized": len(candidates), "included": len(objects),
                         "omitted_by_budget": max(0, len(candidates) - len(objects))}})
        trace = projector._mint(payload)
        return ContextPlan(policy, payload, tuple(roots), reads.read_set, self._issuer,
                           cut.derived_revision, trace)
