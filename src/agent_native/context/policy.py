"""Immutable policy inputs and exported envelopes; no app projection callbacks."""
from dataclasses import dataclass, field
from collections.abc import Mapping

from agent_native.schema import EntityKey, Ref, CollectionRef
from agent_native.state import FrozenMap, FrozenList


class ContextUnavailable(RuntimeError):
    """Public failure deliberately coalesces hidden/absent/integrity diagnostics."""
    def __init__(self):
        super().__init__("context projection unavailable")


@dataclass(frozen=True, slots=True)
class PlanBudget:
    max_objects: int = 32
    max_fields: int = 128
    max_depth: int = 3

    def __post_init__(self):
        if any(type(v) is not int or v < 0 for v in (
                self.max_objects, self.max_fields, self.max_depth)):
            raise TypeError("Use exact nonnegative context limits")


@dataclass(frozen=True, slots=True)
class PolicyRules:
    """Trusted product policy, shared by structured export and C4 provenance.

    export_scope/metadata_scope live on the original Registry declarations and
    Field(json_schema_extra=...). These are policy annotations, not root lists.
    Unannotated supported semantic fields have the public scope. No opaque
    arbitrary Python objects are supported by C1.
    """
    scopes: tuple[str, ...] = ("public",)
    hidden_entities: tuple[EntityKey, ...] = ()
    hidden_fields: tuple[tuple[str, str], ...] = ()

    def __post_init__(self):
        if (type(self.scopes) is not tuple or any(type(v) is not str or not v for v in self.scopes)
                or type(self.hidden_entities) is not tuple
                or any(type(v) is not EntityKey for v in self.hidden_entities)
                or type(self.hidden_fields) is not tuple
                or any(type(v) is not tuple or len(v) != 2
                       or any(type(s) is not str or not s for s in v) for v in self.hidden_fields)):
            raise TypeError("Policy must contain detached immutable scope/identity tuples")

    def scope(self, metadata, *, metadata_only=False):
        key = "metadata_scope" if metadata_only else "export_scope"
        scope = metadata.get(key, metadata.get("export_scope", "public"))
        return type(scope) is str and scope in self.scopes

    def permits(self, descriptor, ref=None, name=None, *, metadata=False):
        if ref is not None and ref.key in self.hidden_entities:
            return False
        if not self.scope(descriptor.metadata):
            return False
        if metadata and not self.scope(descriptor.metadata, metadata_only=True):
            return False
        if name is None:
            return True
        if (descriptor.definition_id, name) in self.hidden_fields:
            return False
        item = next((f for f in descriptor.fields if f.name == name), None)
        if item is None:
            return False
        extra = item.metadata.get("extra", {})
        return (self.scope(extra) and (not metadata or self.scope(extra, metadata_only=True)))

    def fragment(self, value, root=None, seen=()):
        """Conservative generated-schema policy, including referenced records."""
        root = value if root is None else root
        if isinstance(value, Mapping):
            if not self.scope(value) or not self.scope(value, metadata_only=True):
                return False
            link = value.get("$ref")
            if link is not None:
                if type(link) is not str or not link.startswith("#/$defs/"):
                    return False
                if link not in seen:
                    target = root.get("$defs", {}).get(link[len("#/$defs/"):])
                    if target is None or not self.fragment(target, root, (*seen, link)):
                        return False
            return all(self.fragment(v, root, seen) for v in value.values())
        if type(value) in (tuple, list):
            return all(self.fragment(v, root, seen) for v in value)
        return True

    def schema(self, descriptor):
        """All-or-nothing generated schema; never a second author schema."""
        return (self.permits(descriptor, metadata=True)
                and all(self.permits(descriptor, name=f.name, metadata=True) for f in descriptor.fields)
                and self.fragment(descriptor.json_schema))


@dataclass(frozen=True, slots=True)
class ContextRequest:
    actor: str
    task: Ref
    grant: Ref
    destination: str
    focus: Ref
    rules: PolicyRules = field(default_factory=PolicyRules)
    budget: PlanBudget = field(default_factory=PlanBudget)

    def __post_init__(self):
        if (any(type(v) is not str or not v for v in (self.actor, self.destination))
                or any(type(v) is not Ref for v in (self.task, self.grant, self.focus))
                or type(self.rules) is not PolicyRules or type(self.budget) is not PlanBudget):
            raise TypeError("Use immutable typed context request inputs")


@dataclass(frozen=True, slots=True)
class PolicyView:
    """Cut-bound policy plus task/grant/ANCESTOR version dependencies.

    Not an authentication capability. Host authentication precedes this API.
    C4 must call the live ContextExportGate immediately before outgoing export;
    retaining this immutable object is never permission to send after revocation.
    """
    request: ContextRequest
    cut: object
    task_generation: int
    allowed: bool
    resources: tuple
    read_set: object
    _issuer: object = field(repr=False, compare=False)

    _private_types: frozenset = field(default_factory=frozenset, repr=False, compare=False)

    _provenance_scalar_fields: frozenset = field(default_factory=frozenset, repr=False, compare=False)

    _text_fields: frozenset = field(default_factory=frozenset, repr=False, compare=False)

    def permits_field(self, ref, name, *, purpose="text"):
        """Snapshot-only typed eligibility; independent of relevance PlanBudget.

        The CURRENT ContextExportGate is still mandatory. This is neither a
        live bearer capability nor pixel/region/derived-provenance proof.
        Only direct authorized source fields are supported. Unknown purposes,
        unnamed/composite text sources and unproved membership fail closed.
        """
        if (purpose not in ("text", "provenance") or not self.allowed
                or type(ref) is not Ref or type(name) is not str or not name
                or ref.key.entity_type in self._private_types
                or ref.key in self.request.rules.hidden_entities):
            return False
        from .bindings import identity
        if ref not in {identity(value) for value in self.resources}:
            return False
        fields = self._text_fields if purpose == "text" else self._provenance_scalar_fields
        return (ref.key.entity_type, name) in fields

    def permits_provenance(self, descriptors, sources):
        """Conservative source eligibility, not a C4 pixel/export proof.

        descriptors remains for API compatibility; eligibility comes from the
        original compiled declaration snapshot, never caller-supplied metadata.
        """
        return self.allowed and all(self.permits_field(ref, name, purpose="provenance") for ref, name in sources)


def public_value(value):
    if type(value) is Ref:
        return value.key.as_dict()
    if type(value) is CollectionRef:
        return value.as_dict()
    if isinstance(value, Mapping):
        return {k: public_value(v) for k, v in value.items()}
    if type(value) in (tuple, list, FrozenList):
        return [public_value(v) for v in value]
    if value is None or type(value) in (str, int, float, bool):
        return value
    raise TypeError("Unsupported exported semantic value")


@dataclass(frozen=True, slots=True)
class ContextPlan:
    policy: PolicyView
    payload: FrozenMap
    root_identities: tuple
    read_set: object
    _issuer: object = field(repr=False, compare=False)

    @property
    def cut(self):
        return self.policy.cut

    _derived_revision: int = field(default=-1, repr=False, compare=False)

    projection_trace: object = field(default=None, repr=False, compare=False)

    def as_dict(self):
        """Only already-filtered payload is outbound; tokens/reasons stay private."""
        return public_value(self.payload)
