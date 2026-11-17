"""Single source-binding interpretation shared by context and native UI."""
from dataclasses import dataclass
from collections.abc import Mapping

from agent_native.schema import Ref, EntityKey, CollectionRef


def identity(value):
    if type(value) in (Ref, CollectionRef):
        return value
    if (not isinstance(value, Mapping) or set(value) != {
            "entity_type", "entity_id", "generation", "collection_field"}):
        raise ValueError("Malformed source identity")
    key = EntityKey(value["entity_type"], value["entity_id"], value["generation"])
    field = value["collection_field"]
    return Ref(key) if field is None else CollectionRef(key, field)


def resource_record(value):
    key = value.key if type(value) is Ref else value.owner
    return {"entity_type": key.entity_type, "entity_id": key.entity_id,
            "generation": key.generation,
            "collection_field": None if type(value) is Ref else value.field}


@dataclass(frozen=True, slots=True)
class BindingSource:
    source: Ref | CollectionRef | None
    field: str | None
    role: str
    path: str
    command_id: str | None
    parent: Ref | None
    label: str
    visible: bool
    enabled: bool


class BindingSources:
    """Descriptors/source identity only. No domain values, fetch or UI callbacks.

    Data fields are direct C1 fields. Binding.path is a stable visual binding
    path, not an executable query/dotted data path. Collection identity and
    scalar field cannot both be present: there is one unambiguous interpretation.
    """
    def __init__(self, registries, private_types, *, binding_type=None):
        self.owners = {d.definition_id: r for r in registries for d in r.descriptors(kind="entity")}
        self.private_types = frozenset(private_types)
        self.binding_type = binding_type

    def source(self, value):
        source = identity(value)
        key = source.key if type(source) is Ref else source.owner
        registry = self.owners.get(key.entity_type)
        if registry is None or key.entity_type in self.private_types:
            raise ValueError("Unsupported domain source")
        if type(source) is CollectionRef:
            registry.validate_collection_ref(source)
        return source

    def resolve(self, values):
        source = None if values["source"] is None else self.source(values["source"])
        field, role = values["field"], values["role"]
        if role not in ("command", "menu") and source is None:
            raise ValueError("Missing bound source")
        if type(source) is CollectionRef:
            if field is not None or role != "collection":
                raise ValueError("Collection source must be one alias without scalar field")
        elif source is not None:
            descriptor = self.owners[source.key.entity_type].entity_descriptor(source.key.entity_type)
            item = next((f for f in descriptor.fields if f.name == field), None)
            if field is not None and item is None:
                raise ValueError("Unknown bound field")
            if role == "collection":
                if field is None:
                    raise ValueError("Collection binding requires its direct Owned field")
                source = self.source(CollectionRef(source.key, field))
                field = None
        parent = None if values.get("parent") is None else identity(values["parent"])
        if parent is not None and (type(parent) is not Ref or parent.key.entity_type != self.binding_type):
            raise ValueError("Parent must identify one canonical Binding")
        if role == "menu" and (source is not None or field is not None or values["command_id"] is not None):
            raise ValueError("Menu is a semantic grouping, not a domain-value copy")
        return BindingSource(source, field, role, values["path"], values["command_id"], parent,
                             values.get("label", ""), values.get("visible", True), values.get("enabled", True))


@dataclass(frozen=True, slots=True)
class ResolvedBinding:
    binding: Ref
    source: BindingSource
    state_ref: Ref | None
    opened: bool
    scroll_y: int
    effective_visible: bool
    effective_enabled: bool


@dataclass(frozen=True, slots=True)
class ViewBindings:
    principal: str
    state: str
    rows: tuple[ResolvedBinding, ...]


def resolve_view_bindings(view, page_ref, sources):
    """One memory-only menu/list interpretation for ContextIndex AND native UI.

    label is an author literal, never a cached domain label. scroll_y is integer
    LOGICAL Qt px; UI clamps with a managed memory intention, not QWidget state.
    Missing menu state refuses export; legacy collection bindings default to 0.
    Offscreen list rows stay logical roots; C4 separately marks not_rendered.
    """
    def memory(ref):
        record = view.resolve(ref)
        if record.partition != "memory":
            raise ValueError("Semantic UI must be memory-only")
        return record.values
    page = memory(page_ref)
    rows, paths = {}, set()
    for ref in page["bindings"]:
        if ref in rows:
            raise ValueError("Duplicate view binding")
        source = sources.resolve(memory(ref))
        if not source.path or source.path in paths:
            raise ValueError("Duplicate/empty binding path")
        paths.add(source.path)
        rows[ref] = source
    states = {}
    for ref in page["states"]:
        state = memory(ref)
        binding = state["binding"]
        if binding not in rows or binding in states:
            raise ValueError("Cross-view or duplicate UI state")
        role = rows[binding].role
        if role == "menu":
            if state["scroll_y"] != 0:
                raise ValueError("Menu cannot own scrolling")
        elif role == "collection":
            if state["opened"]:
                raise ValueError("Collection cannot own menu disclosure")
        else:
            raise ValueError("State belongs to unsupported binding role")
        states[binding] = (ref, state["opened"], state["scroll_y"])
    computed = {}
    def effective(ref, stack=()):
        if ref in computed:
            return computed[ref]
        if ref in stack or len(stack) >= 16:
            raise ValueError("Binding parent cycle/depth")
        source = rows[ref]
        state = states.get(ref, (None, False, 0))
        if source.role == "menu" and state[0] is None:
            raise ValueError("Menu requires canonical UiState")
        visible, enabled = source.visible, source.enabled
        if source.parent is not None:
            if source.parent not in rows:
                raise ValueError("Parent belongs to another view")
            parent = effective(source.parent, (*stack, ref))
            visible = visible and parent.effective_visible
            enabled = enabled and parent.effective_enabled
            if parent.source.role == "menu":
                visible = visible and parent.opened
        result = ResolvedBinding(ref, source, *state, visible, enabled)
        computed[ref] = result
        return result
    return ViewBindings(page["principal"], page["state"],
                        tuple(effective(ref) for ref in rows))
