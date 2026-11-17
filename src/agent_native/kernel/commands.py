"""S1 command admission over the one C1 graph and Coordinator.

All mutating commands preview then apply the SAME RAW input: one author handler,
two StateStore normalization runs. Approval adds a system-only pending transition.
No model code, network, SQL, hidden rebase or handler replay is performed here.
Trusted Python callbacks remain an author contract, not a security sandbox.
"""
from dataclasses import dataclass, replace
from inspect import iscoroutinefunction, isgeneratorfunction, isasyncgenfunction
from time import monotonic_ns
from uuid import uuid4
from weakref import WeakValueDictionary

from agent_native.schema import EntityKey, Ref, CollectionRef
from agent_native.state import Patch, Create, Update, thaw, deep_freeze, strict_equal
from agent_native.state.tracking import ReadTracker
from agent_native.state.derived import _result
from .coordinator import (BusyPolicy, Phase, Status, SubmissionResult, CoordinatorUnavailable, PreparedChange, PreparedPreview,
    PreviewStatus, PreviewCandidate, StagedAcceptance, AcceptedDecision, RejectedDecision)
from .system_definitions import SystemDefinitions, UiSystemDefinitions
from ._grant_authority import GrantAuthority, GrantIntegrityError
from . import _command_values as v
from ._prepare_origin import PrepareOriginFacts,capture_prepare_origin,check_prepare_origin
from .permissions import (CommandPermissionRequest, CommandPermissionLimit, ResolvedCommandPermissions,
    PermissionError, catalog as permission_catalog, policy_callback, resolve as resolve_permissions)

PROTOCOL = "agent-native.commands/1"
RAW = "agent-native.raw-plan/1"
CANDIDATE = "agent-native.candidate/1"
DEPS = "agent-native.dependencies/1"
PROVENANCE = "agent-native.provenance/1"


class AdmissionError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True, weakref_slot=True)
class TrustedOrigin:
    """Host-issued authentication assertion; never parsed from model JSON."""
    principal: str
    channel: str
    _issuer: object


@dataclass(frozen=True, slots=True, weakref_slot=True)
class DeliveryContext:
    principal: str
    task: Ref
    request_id: str
    call_slot: str
    grant: Ref
    _issuer: object

    @property
    def key(self):
        k = self.task.key
        return (k.entity_type, k.entity_id, k.generation, self.request_id, self.call_slot)


@dataclass(frozen=True, slots=True)
class IssuedBasis:
    handle: str
    principal: str
    kind: str
    task: Ref
    task_generation: int
    request_id: str
    cut: object
    deadline_ns: int
    _issuer: object


@dataclass(frozen=True, slots=True)
class SystemBasis:
    """Engine-only pinned current cut. Not accepted by domain Proposal."""
    principal: str
    cut: object
    _issuer: object


@dataclass(frozen=True, slots=True)
class EffectSpec:
    resource: Ref | CollectionRef
    action: str
    payload: object = None
    stop_activity: Ref | None = None


@dataclass(frozen=True, slots=True)
class DelegationSpec:
    """Trusted request for ONE NEW Activity; kernel owns every private identity."""
    activity: Ref
    resources: tuple[Ref | CollectionRef, ...]
    actions: tuple[str, ...]
    create_types: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Plan:
    patch: Patch
    effects: tuple[EffectSpec, ...] = ()
    delegations: tuple[DelegationSpec, ...] = ()


class PrepareContext:
    __slots__ = ("view", "plan_id", "_origin")

    def __init__(self, view, plan_id, *, _origin=None):
        if _origin is not None and type(_origin) is not PrepareOriginFacts:
            raise TypeError("Use the frozen SDK origin DATA")
        self.view, self.plan_id, self._origin = view, plan_id, _origin

    @property
    def origin(self):
        """Readonly checked C2 facts; never a current domain reader/capability."""
        return self._origin

    def stable_id(self, name):
        v.text(name)
        return self.plan_id + "-" + name

    def new_ref(self, entity, name):
        typ = entity if type(entity) is str else entity.__entity_type__
        return Ref(EntityKey(typ, self.stable_id(name), 1))


@dataclass(frozen=True, slots=True)
class CommandReceipt:
    plan_id: str
    outcome: str
    digest: str
    invocation: Ref | None
    ticket: Ref | None
    effects: tuple


@dataclass(frozen=True, slots=True)
class CommandResult:
    outcome: str
    receipt: CommandReceipt | None = None
    submission: object = None
    error: str | None = None
    new_flow_required: bool = False


@dataclass(frozen=True, slots=True)
class SystemResult:
    ref: Ref
    submission: object
    permissions: ResolvedCommandPermissions | None = None


_CONFLICTS = frozenset({
    "conflict", "policy_changed", "stale_task", "stale_request", "mutation_slot_unavailable",
    "approval_request_changed", "grant_changed", "create_identity_exists", "create_identity_changed",
    "approval_candidate_changed", "approval_ticket_changed", "approval_expired", "basis_expired",
    "basis_request_mismatch", "schema_changed", "resource_generation_unavailable",
})
_DENIALS = frozenset({
    "unauthenticated_origin", "forged_delivery", "forged_basis", "origin_basis_mismatch",
    "receipt_read_denied", "basis_unavailable", "grant_denied", "command_not_granted",
    "normalized_scope_not_granted", "creation_not_granted", "effect_not_granted", "task_denied",
    "approval_denied", "approval_digest_mismatch", "private_system_write", "invalid_domain_resource",
    "grant_integrity_denied", "command_profile_denied", "delegation_denied",
    "runner_response_unavailable", "runner_response_invalid", "runner_response_not_executable", "runner_response_selection_mismatch",
    "command_policy_denied", "aggregate_command_policy_conflict",
    "program_information_flow_denied", "program_admission_denied", "program_approval_unsupported",
})
_INVALID = frozenset({
    "invalid_proposal_envelope", "invalid_arguments", "unknown_command", "invalid_approval_policy",
    "unsupported_delegation", "approval_payload_changed", "incomplete_readset", "system_projection_changed",
})


def _rejected(code, *, submission=None, default="failed"):
    """Phase-independent public classification of known C2 refusal codes."""
    outcome = ("conflict" if code in _CONFLICTS else "denied" if code in _DENIALS else
               "failed" if code in _INVALID else "reconciling" if code == "journal_index_unavailable" else
               "busy" if code in ("semantic_guarded", "failure_tracking_quota") else default)
    return CommandResult(outcome, submission=submission, error=code)


class CommandKernel:
    """Trusted host facade, not a provider tool executor.

    authenticated() asserts authentication already performed by the host. It does
    not authenticate a password/provider. Same-principal receipt reads are the
    default; a trusted receipt_policy may further restrict them.
    """
    def __init__(self, coordinator, registries, definitions: SystemDefinitions, *,
                 journal=(), clock_ns=monotonic_ns, basis_ttl_ns=30_000_000_000,
                 max_bases=128, max_failures=128, approval_ttl_ns=120_000_000_000, receipt_policy=None, approval_policy=None, command_policy=None,
                 project_id="default", storage_id="primary"):
        if type(definitions) not in (SystemDefinitions, UiSystemDefinitions):
            raise TypeError("Register real system definitions before constructing C1")
        self.coordinator, self.system = coordinator, definitions
        from ._validation import name
        name(project_id); name(storage_id)
        self._runtime_scope = (project_id, storage_id)
        self.registries = tuple(registries)
        self._entities, self._commands = {}, {}
        for registry in self.registries:
            for d in registry.descriptors(kind="entity"):
                if d.definition_id in self._entities:
                    raise ValueError("Duplicate entity definition")
                self._entities[d.definition_id] = registry
            for d in registry.descriptors(kind="command"):
                if d.definition_id in self._commands:
                    raise ValueError("Duplicate command definition")
                self._commands[d.definition_id] = registry
        self._system_types = {d.definition_id for d in definitions.registry.descriptors(kind="entity")}
        if not self._system_types.issubset(self._entities):
            raise ValueError("System definitions must be registered BEFORE StateStore")
        self._permission_profiles = permission_catalog(self.registries, self._system_types)
        self._command_policy = policy_callback(command_policy)
        self.grant_authority = GrantAuthority(definitions, self.registries)
        with coordinator.read() as cut:
            self.grant_authority.validate_graph(cut.view)
        self._issuer = object()
        self._origins, self._delivery_capabilities = WeakValueDictionary(), WeakValueDictionary()
        self._failures, self._failure_reservations = {}, {}
        self._max_failures = v.integer(max_failures, 1)
        self._clock = clock_ns
        self._approval_ttl = v.integer(approval_ttl_ns, 1)
        self._ttl, self._max_bases = v.integer(basis_ttl_ns, 1), v.integer(max_bases, 1)
        for callback in (receipt_policy, approval_policy):
            if callback is not None and (not callable(callback) or iscoroutinefunction(callback)
                    or isgeneratorfunction(callback) or isasyncgenfunction(callback)):
                raise TypeError("Policies must be trusted synchronous callbacks")
        self._receipt_policy, self._approval_policy = receipt_policy, approval_policy
        self._bases, self._deliveries, self._pending, self._transactions = {}, {}, {}, {}
        self._runner_bridge = None
        self._program_candidates = {}
        self._index_valid, self._closed, self._journal_revision = True, False, 0
        self._known_commits = set()
        self.refresh_journal(journal)
        coordinator.subscribe(self._published)

    def authenticated(self, principal, *, channel="host"):
        """Host only. Model-facing parsers never call this method."""
        v.text(principal)
        if channel not in ("host", "agent", "ui"):
            raise ValueError("Unknown trusted channel")
        origin = TrustedOrigin(principal, channel, self._issuer)
        self._origins[id(origin)] = origin
        return origin

    def _auth(self, origin, *, host=False):
        if (self._closed or type(origin) is not TrustedOrigin or origin._issuer is not self._issuer
                or self._origins.get(id(origin)) is not origin
                or (host and origin.channel != "host")):
            raise AdmissionError("unauthenticated_origin")

    def _ref(self, model, ident):
        return Ref(EntityKey(model.__entity_type__, ident, 1))

    def _request_ref(self, request):
        return self._ref(self.system.RequestAdmission, request)

    def _system_submit(self, origin, build, *, committed=None, on_terminal=None, verify=None):
        """Private SDK system transition; original verification is mandatory.

        Optional verify runs only AFTER the same system/authority checks. Its
        only override is a rejection, never replacement of the accepted bundle.
        on_terminal retains actual correlation under NOTIFYING; no reads, clock,
        capture or semantic writes are permitted there. Exceptions keep ordinary
        Coordinator terminal retry/reconciliation semantics, without SQL replay.
        Existing zero-argument committed callbacks retain their default behavior.
        """
        self._auth(origin, host=True)
        def acceptance(expectation, inputs):
            decision = self._verify_system(expectation, inputs)
            if type(decision) is RejectedDecision or verify is None:
                return decision
            extra = verify(expectation, inputs)
            self.coordinator._reject_async(extra)
            if type(extra) is RejectedDecision:
                return extra
            if extra is not None and type(extra) is not AcceptedDecision:
                raise TypeError("SDK system verifier must return a decision or None")
            return decision
        def prepare(current):
            with self.coordinator.read() as cut:
                basis = SystemBasis(origin.principal, cut, self._issuer)
                patch = build(current, basis)
                if self.system.supports_agent_runtime:
                    from ._conversation import append_conversation_events
                    patch = append_conversation_events(self.system, cut.view, patch)
                raw = v.patch_data(patch)
            return PreparedChange(v.patch(raw), acceptance=StagedAcceptance(v.encode(raw), acceptance))
        def notify(result):
            if on_terminal is not None:
                returned = on_terminal(result)
                self.coordinator._reject_async(returned)
                if returned is not None:
                    raise TypeError("SDK terminal notification must return None")
            if result.status == Status.COMMITTED and committed is not None:
                committed()
        return self.coordinator.submit("system-" + uuid4().hex, prepare,
            memory_only=False, busy=BusyPolicy.REJECT,
            on_terminal=notify if committed is not None or on_terminal is not None else None)

    @staticmethod
    def _system_matches(raw, inputs):
        actual, _ = v.projection(raw, inputs.base_snapshot, inputs.staged)
        for entry in actual:
            if entry[0] == "create":
                source = next(x[2] for x in raw["creates"] if x[0] == entry[1].key.entity_type
                              and x[1] == entry[1].key.entity_id)
                if entry[1].key.generation != 1 or v.digest(entry[3]) != v.digest(source):
                    return False
            elif entry[0] == "update":
                previous = thaw(inputs.base_snapshot.records[entry[1].key].values)
                previous.update(next(changes for ref, changes in raw["updates"] if ref == entry[1]))
                if v.digest(previous) != v.digest(entry[3]):
                    return False
        return True

    def _verify_system(self, expectation, inputs):
        if not self._system_matches(v.decode(expectation), inputs):
            return RejectedDecision("system_projection_changed")
        try:
            self.grant_authority.validate_transition(inputs.base.view, inputs.staged)
        except GrantIntegrityError as error:
            return RejectedDecision("grant_integrity_denied", error.code)
        return AcceptedDecision()

    def _require_agent_runtime(self):
        if not self.system.supports_agent_runtime:
            raise AdmissionError("unsupported_system_profile")

    def create_conversation(self, origin, *, conversation_id=None):
        """Host-owned conversation in this kernel's actual project/storage scope."""
        self._require_agent_runtime()
        from ._conversation import conversation_values
        ref = self._ref(self.system.Conversation, conversation_id or uuid4().hex)
        def build(current, basis):
            return Patch(creates=(Create(ref.key.entity_type, ref.key.entity_id,
                conversation_values(origin.principal, *self._runtime_scope)),))
        return SystemResult(ref, self._system_submit(origin, build))

    def lookup_skill_control(self, origin, operation, request_id, input_digest):
        self._require_agent_runtime()
        from ._skill_control import lookup_skill_control
        return lookup_skill_control(self, origin, operation, request_id, input_digest)

    def prepare_skill_control(self, origin, operation, request_id, input_digest, *, grant):
        self._require_agent_runtime()
        from ._skill_control import prepare_skill_control
        return prepare_skill_control(self, origin, operation, request_id, input_digest, grant=grant)

    def pin_program_attempt(self, origin, attempt_ref):
        self._require_agent_runtime()
        from ._programs import pin_program_attempt
        return pin_program_attempt(self, origin, attempt_ref)

    def admit_program_step(self, origin, attempt_ref, canonical_step):
        self._require_agent_runtime()
        from ._programs import admit_program_step
        return admit_program_step(self, origin, attempt_ref, canonical_step)

    def commit_summary(self, origin, attempt_ref, summary_record):
        self._require_agent_runtime()
        from ._conversation import commit_summary
        return commit_summary(self, origin, attempt_ref, summary_record)

    def create_task(self, origin, *, task_id=None):
        ref = self._ref(self.system.TaskSession, task_id or uuid4().hex)
        def build(current, basis):
            return Patch(creates=(Create(ref.key.entity_type, ref.key.entity_id, {
                "principal": basis.principal, "generation": 1, "state": "active", "active_request": None, "working_resources": ()}),))
        return SystemResult(ref, self._system_submit(origin, build))

    def issue_grant(self, origin, task, *, resources=(), actions=(), create_types=(),
                    allow_activity_transfer=False, grant_id=None):
        ref = self._ref(self.system.Grant, grant_id or uuid4().hex)
        if type(allow_activity_transfer) is not bool:
            raise TypeError("Transfer permission must be an explicit exact boolean")
        resources = tuple(resources)
        resource_ids = set()
        for resource in resources:
            self._resource_schema(resource)
            resource_ids.update(v.resources(resource))
        values = tuple({"entity_type": a, "entity_id": b, "generation": c, "collection_field": d}
                       for a, b, c, d in sorted(resource_ids, key=repr))
        actions, create_types = tuple(actions), tuple(create_types)
        for action in actions:
            v.text(action)
        for typ in create_types:
            if typ not in self._entities or typ in self._system_types:
                raise ValueError("Unknown or private creation type")
        def build(current, basis):
            record = current.resolve(task)
            if record.values["principal"] != basis.principal or record.values["state"] != "active":
                raise AdmissionError("task_denied")
            for resource in resources:
                if type(resource) is Ref:
                    current.resolve(resource)
                else:
                    current.resolve_collection(resource).members
            return Patch(creates=(Create(ref.key.entity_type, ref.key.entity_id, {
                "principal": basis.principal, "task": task, "task_generation": record.values["generation"],
                "owner_activity": None, "kind": "authority", "resources": values, "actions": actions,
                "create_types": create_types, "revoked": False, "parent": None, "plan_id": None,
                "allow_activity_transfer": allow_activity_transfer, "created_resources": (),
                "source_invocation": None}),))
        return SystemResult(ref, self._system_submit(origin, build))

    def issue_read_grant(self, origin, task, *, resources=(), grant_id=None):
        """Issue a real read-only C1 authority grant for the exact host scope.

        No command policy is consulted because no command/effect/create/transfer
        right is requested. Actual task, resource and graph checks are identical
        to ordinary host grant issuance. The returned scope is only a detached
        expectation; every use must still validate the current Grant record.
        """
        self._auth(origin, host=True)
        if type(resources) is not tuple or len(resources) > 1024:
            raise TypeError("Use a bounded immutable resource tuple")
        if any(type(item) not in (Ref, CollectionRef) for item in resources):
            raise TypeError("Use typed resource identities")
        result = self.issue_grant(origin, task, resources=resources, grant_id=grant_id)
        scope = ResolvedCommandPermissions((), (), (), False, resources)
        return SystemResult(result.ref, result.submission,
            scope if result.submission.status in (Status.COMMITTED, Status.AWAITING_DB, Status.RECONCILING) else None)

    def issue_program_authority(self, origin, task, *, commands, resources, grant_id=None):
        """Explicit host policy grant for the first local program profile.

        Domain rights come from the same current command-policy resolver. The
        only added framework rights are engine.program.run, creation of the
        exact declared ExtensionActivation, and Activity transfer. This ROOT
        grant is a lawful future anchor parent, not proof of installed code.
        Ordinary app/model commands still cannot create private SYS entities.
        """
        self._require_agent_runtime()
        self._auth(origin, host=True)
        if type(commands) is not tuple or type(resources) is not tuple:
            raise TypeError("Use immutable command/resource tuples")
        for resource in resources:
            self._resource_schema(resource)
        ref = self._ref(self.system.Grant, grant_id or uuid4().hex)
        resolved = []
        def build(current, basis):
            task_values = current.resolve(task).values
            if task_values["principal"] != origin.principal or task_values["state"] != "active":
                raise AdmissionError("task_denied")
            scope = resolve_permissions(self._permission_profiles, self._command_policy,
                principal=origin.principal, channel="agent", task=task, commands=commands, resources=resources)
            identities = set()
            for resource in resources:
                if type(resource) is Ref:
                    current.resolve(resource)
                else:
                    current.resolve_collection(resource).members
                identities.update(v.resources(resource))
            resolved.append(scope)
            values = {"principal": origin.principal, "task": task, "task_generation": task_values["generation"],
                "owner_activity": None, "kind": "authority",
                "resources": tuple({"entity_type":a,"entity_id":b,"generation":c,"collection_field":f}
                    for a,b,c,f in sorted(identities,key=repr)),
                "actions": tuple(sorted(set(scope.actions) | {"engine.program.run"})),
                "create_types": tuple(sorted(set(scope.create_types) | {self.system.ExtensionActivation.__entity_type__})),
                "revoked": False, "parent": None, "plan_id": None, "allow_activity_transfer": True,
                "created_resources": (), "source_invocation": None}
            return Patch(creates=(Create(ref.key.entity_type,ref.key.entity_id,values),))
        submission = self._system_submit(origin, build)
        return SystemResult(ref, submission, resolved[-1] if resolved else None)

    def issue_command_grant(self, origin, task, *, channel, commands, resources, grant_id=None):
        """Host-only SAME-profile issuance. Composite closure fails as a whole.

        permissions is a detached expected scope for positive C1 resume checks,
        never an independent capability or a replacement for actual Grant state.
        """
        self._auth(origin, host=True)
        if type(commands) is not tuple or type(resources) is not tuple:
            raise TypeError("Use immutable command/resource tuples")
        ref = self._ref(self.system.Grant, grant_id or uuid4().hex)
        for resource in resources:
            self._resource_schema(resource)
        resolved, failures = [], []
        def build(current, basis):
            task_record = current.resolve(task)
            if (task_record.values["principal"] != basis.principal
                    or task_record.values["state"] != "active"):
                raise AdmissionError("task_denied")
            try:
                scope = resolve_permissions(self._permission_profiles, self._command_policy,
                    principal=basis.principal, channel=channel, task=task,
                    commands=commands, resources=resources)
            except PermissionError as error:
                failures.append(error.code)
                raise AdmissionError(error.code) from None
            identities = set()
            for resource in resources:
                if type(resource) is Ref:
                    current.resolve(resource)
                else:
                    current.resolve_collection(resource).members
                identities.update(v.resources(resource))
            resolved.append(scope)
            values = tuple({"entity_type": a, "entity_id": b, "generation": c, "collection_field": d}
                           for a, b, c, d in sorted(identities, key=repr))
            return Patch(creates=(Create(ref.key.entity_type, ref.key.entity_id, {
                "principal": basis.principal, "task": task,
                "task_generation": task_record.values["generation"], "owner_activity": None,
                "kind": "authority", "resources": values, "actions": scope.actions,
                "create_types": scope.create_types, "revoked": False, "parent": None,
                "plan_id": None, "allow_activity_transfer": scope.allow_activity_transfer,
                "created_resources": (), "source_invocation": None}),))
        submission = self._system_submit(origin, build)
        if failures and submission.status == Status.FAILED:
            submission = replace(submission, error=failures[-1])
        return SystemResult(ref, submission, resolved[-1] if resolved else None)

    def _agent_runtime(self, origin):
        self._require_agent_runtime()
        self._auth(origin, host=True)
        if self._runner_bridge is None:
            self.coordinator._idle()
            from ._agent_requests import AgentRequests
            self._runner_bridge = AgentRequests(self)
        return self._runner_bridge

    def create_runner_session(self, origin, task, grant, **spec):
        return self._agent_runtime(origin).create_session(origin, task, grant, **spec)

    def reserve_agent_request(self, origin, task, request_id, *, deadline_ns=None):
        return self._agent_runtime(origin).reserve(origin, task, request_id, deadline_ns=deadline_ns)

    def release_agent_request(self, origin, reservation):
        return self._agent_runtime(origin).release(origin, reservation)

    def prepare_agent_request(self, origin, reservation, *, consumption, spec):
        return self._agent_runtime(origin).prepare(origin, reservation, consumption=consumption, spec=spec)

    def seal_agent_request(self, origin, preparation, *, terminal):
        return self._agent_runtime(origin).seal(origin, preparation, terminal=terminal)

    def abandon_agent_request(self, origin, preparation):
        return self._agent_runtime(origin).abandon(origin, preparation)

    def bind_request_export(self, origin, backend):
        return self._agent_runtime(origin).bind_export(origin, backend)

    def arm_agent_send(self, origin, prepared):
        return self._agent_runtime(origin).arm(origin, prepared)

    def consume_agent_send(self, origin, arm, *, terminal):
        return self._agent_runtime(origin).consume(origin, arm, terminal=terminal)

    def cancel_agent_session(self, origin, session, *, no_send_plan):
        return self._agent_runtime(origin).cancel_session(origin, session, no_send_plan=no_send_plan)

    def bind_local_cancellation(self, origin, backend):
        return self._agent_runtime(origin).bind_local(origin, backend)

    def issue_basis(self, origin, task, request_id, *, kind="agent"):
        self._auth(origin, host=True)
        v.text(request_id)
        if kind not in ("agent", "ui"):
            raise AdmissionError("unsupported_basis_kind")
        if self.coordinator.phase != Phase.IDLE:
            raise AdmissionError("semantic_guarded")
        self._prune_expired_bases()
        reserved = len(self._runner_bridge._reservations) if self._runner_bridge is not None else 0
        if len(self._bases) + reserved >= self._max_bases:
            raise AdmissionError("basis_quota")
        cut = self.coordinator.read()
        try:
            record = cut.view.resolve(task)
            if record.values["principal"] != origin.principal or record.values["state"] != "active":
                raise AdmissionError("task_denied")
            handle = uuid4().hex
            basis = IssuedBasis(handle, origin.principal, kind, task, record.values["generation"],
                                request_id, cut, self._clock() + self._ttl, self._issuer)
            self._bases[handle] = basis
            return basis
        except BaseException:
            cut.close()
            raise

    def release_basis(self, handle):
        # Invalidate the capability BEFORE forgetting any negative-delivery identity.
        basis = self._bases.pop(handle, None)
        self._program_candidates.pop(handle, None)
        if basis is not None:
            basis.cut.close()
        for key, failure in tuple(self._failures.items()):
            if failure["basis"] == handle:
                del self._failures[key]

    def _retire_bases(self, task, *, keep_request=None):
        for handle, basis in tuple(self._bases.items()):
            if basis.task == task and basis.request_id != keep_request:
                self.release_basis(handle)

    def _prune_expired_bases(self):
        if self._runner_bridge is not None:
            self._runner_bridge.prune()
        for handle, basis in tuple(self._bases.items()):
            if self._clock() >= basis.deadline_ns or not basis.cut.view.available:
                self.release_basis(handle)

        for key, failure in tuple(self._failures.items()):
            if failure["basis"] not in self._bases:
                del self._failures[key]

    def _failure_capacity(self, key):
        self._prune_expired_bases()
        if (key not in self._failures and key not in self._failure_reservations
                and len(self._failures) + len(self._failure_reservations) >= self._max_failures):
            raise AdmissionError("failure_tracking_quota")

    def _reserve_failure(self, delivery, handle):
        self._failure_capacity(delivery.key)
        if delivery.key not in self._failures:
            self._failure_reservations[delivery.key] = handle

    def _require_index(self):
        if not self._index_valid:
            raise AdmissionError("journal_index_unavailable")

    def open_request(self, origin, basis, *, call_slots=("0",)):
        self._auth(origin, host=True)
        if self._basis(basis.handle, origin.principal) is not basis:
            raise AdmissionError("forged_basis")
        call_slots = tuple(call_slots)
        if not call_slots or len(set(call_slots)) != len(call_slots):
            raise ValueError("Use unique trusted call slots")
        for slot in call_slots:
            v.text(slot)
        ref = self._request_ref(basis.request_id)
        def build(current, system_basis):
            task = current.resolve(basis.task)
            if task.values["generation"] != basis.task_generation or task.values["state"] != "active":
                raise AdmissionError("stale_task")
            return Patch(creates=(Create(ref.key.entity_type, ref.key.entity_id, {
                "task": basis.task, "task_generation": basis.task_generation, "request_id": basis.request_id,
                "basis_handle": basis.handle, "basis_kind": basis.kind, "program_attempt": None, "call_slots": call_slots,
                "state": "open", "approval_id": None, "invocation_id": None}),),
                updates=(Update(basis.task, {"active_request": basis.request_id}),))
        return SystemResult(ref, self._system_submit(origin, build,
            committed=lambda: self._retire_bases(basis.task, keep_request=basis.request_id)))

    def revoke_grant(self, origin, grant):
        def build(current, basis):
            if current.field(grant, "principal") != basis.principal:
                raise AdmissionError("grant_denied")
            return Patch(updates=(Update(grant, {"revoked": True}),))
        return SystemResult(grant, self._system_submit(origin, build))

    def end_task(self, origin, task, *, cancel=False):
        def build(current, basis):
            if cancel and any(current.field(ref, "task") == task
                    for ref in current.entities(self.system.RunnerSession.__entity_type__)):
                raise AdmissionError("runner_cancellation_control_required")
            record = current.resolve(task)
            if record.values["principal"] != basis.principal:
                raise AdmissionError("task_denied")
            return Patch(updates=(Update(task, {"state": "cancelled" if cancel else "completed",
                "generation": record.values["generation"] + (1 if cancel else 0), "active_request": None, "working_resources": ()}),))
        return SystemResult(task, self._system_submit(origin, build,
            committed=lambda: self._retire_bases(task)))

    def delivery_context(self, origin, basis, grant, *, call_slot="0"):
        self._auth(origin)
        if (type(basis) is not IssuedBasis or basis._issuer is not self._issuer
                or self._bases.get(basis.handle) is not basis):
            raise AdmissionError("forged_basis")
        if basis.principal != origin.principal or origin.channel != basis.kind:
            raise AdmissionError("origin_basis_mismatch")
        self._basis(basis.handle, origin.principal)
        v.text(call_slot)
        return self.resume_delivery(origin, basis.task, basis.request_id, grant, call_slot=call_slot)

    def resume_delivery(self, origin, task, request_id, grant, *, call_slot="0"):
        """Trusted C5 reconnects saved delivery identity; this does not revive its basis."""
        self._auth(origin)
        if type(task) is not Ref or type(grant) is not Ref:
            raise TypeError("Use typed task/grant identities")
        v.text(request_id)
        v.text(call_slot)
        return self._new_delivery(origin.principal, task, request_id, call_slot, grant)

    def _new_delivery(self, principal, task, request_id, call_slot, grant):
        k = task.key
        key = (k.entity_type, k.entity_id, k.generation, request_id, call_slot)
        if key not in self._deliveries:
            self._failure_capacity(key)
        delivery = DeliveryContext(principal, task, request_id, call_slot, grant, self._issuer)
        self._delivery_capabilities[id(delivery)] = delivery
        return delivery

    def _context(self, origin, delivery):
        self._auth(origin)
        if (type(delivery) is not DeliveryContext or delivery._issuer is not self._issuer
                or delivery.principal != origin.principal
                or self._delivery_capabilities.get(id(delivery)) is not delivery):
            raise AdmissionError("forged_delivery")

    def _basis(self, handle, principal):
        if type(handle) is not str:
            raise AdmissionError("forged_basis")
        basis = self._bases.get(handle)
        if basis is None or basis._issuer is not self._issuer or basis.principal != principal:
            raise AdmissionError("basis_unavailable")
        if (self._clock() >= basis.deadline_ns or not basis.cut.view.available
                or not self.coordinator.validate_cut(basis.cut)):
            raise AdmissionError("basis_expired")
        return basis

    def _authorize_receipt(self, origin, delivery):
        entry = self._deliveries.get(delivery.key)
        pending = self._pending.get(delivery.key)
        if entry is not None:
            owner = entry["principal"]
        elif pending is not None:
            owner = pending["principal"]
        elif delivery.key in self._failures:
            owner = self._failures[delivery.key]["principal"]
        else:
            with self.coordinator.read() as cut:
                owner = cut.view.field(delivery.task, "principal")
        self._receipt_authorized(origin, owner)

    def _receipt_authorized(self, origin, owner):
        allowed = owner == origin.principal
        if allowed and self._receipt_policy is not None:
            allowed = v.sync(self._receipt_policy(origin.principal, owner))
            if type(allowed) is not bool:
                raise TypeError("Receipt policy must return exact bool")
        if not allowed:
            raise AdmissionError("receipt_read_denied")

    @staticmethod
    def _receipt(entry):
        receipt = deep_freeze(entry["receipt"])
        return CommandReceipt(receipt["plan_id"], receipt["outcome"], receipt["digest"],
                              receipt["invocation"], receipt["ticket"], receipt["effects"])

    def receipt(self, origin, delivery):
        try:
            self._context(origin, delivery)
            self._authorize_receipt(origin, delivery)
            self._require_index()
            entry = self._deliveries.get(delivery.key)
            failure = self._failures.get(delivery.key)
            if failure is not None and (entry is None or entry["receipt"]["invocation"] is None):
                return CommandResult("failed_no_effect", error=failure["error"], new_flow_required=True)
            if entry is not None:
                return CommandResult(entry["receipt"]["outcome"], self._receipt(entry))
            if delivery.key in self._pending:
                return CommandResult("reconciling" if self.coordinator.phase == Phase.RECONCILING
                                     else "awaiting_db")
            return CommandResult("not_found")
        except CoordinatorUnavailable:
            return CommandResult("reconciling", error="coherent_authority_unavailable")
        except AdmissionError as error:
            return _rejected(error.code, default="denied")
        except (ValueError, TypeError, LookupError) as error:
            return CommandResult("denied", error=type(error).__name__)

    def _raw_proposal(self, proposal):
        if (type(proposal) is not dict or set(proposal) != {
                "command", "schema_revision", "schema_fingerprint", "basis", "arguments"}
                or type(proposal["arguments"]) is not dict):
            raise AdmissionError("invalid_proposal_envelope")
        try:
            v.text(proposal["command"])
            v.integer(proposal["schema_revision"], 1)
            v.text(proposal["schema_fingerprint"])
            v.text(proposal["basis"])
        except (TypeError, ValueError) as error:
            raise AdmissionError("invalid_proposal_envelope") from error
        return v.decode(v.encode(proposal))

    def _schema(self, proposal):
        registry = self._commands.get(proposal["command"])
        if registry is None:
            raise AdmissionError("unknown_command")
        descriptor = registry.command_descriptor(proposal["command"])
        if (descriptor.revision != proposal["schema_revision"]
                or descriptor.fingerprint != proposal["schema_fingerprint"]):
            raise AdmissionError("schema_changed")
        return registry, descriptor

    def _runner_response(self, delivery, view, proposal=None):
        if not self.system.supports_agent_runtime:
            return
        model = self._ref(self.system.ModelRequest, delivery.request_id)
        if not view.exists(model):
            return
        request = thaw(view.resolve(model).values)
        if request["purpose"] != "act":
            raise AdmissionError("runner_response_not_executable")
        response_ref = self._ref(self.system.AcceptedModelResponse, delivery.request_id)
        if (request["state"] not in ("response_accepted", "closed")
                or request["response_fingerprint"] is None or not view.exists(response_ref)):
            raise AdmissionError("runner_response_unavailable")
        response = thaw(view.resolve(response_ref).values)
        from agent_native.runner.protocol import decode_response_envelope, decide_response
        try:
            checked = decode_response_envelope(response["body"], output_reservation=request["reservation"]["output_tokens"])
            decision = decide_response(checked, command_ids=tuple(self._commands))
        except (TypeError, ValueError, UnicodeError):
            raise AdmissionError("runner_response_invalid") from None
        if (response["request"] != model or response["fingerprint"] != request["response_fingerprint"]
                or checked.fingerprint != response["fingerprint"] or response["accepted_as"] != "active"
                or decision.outcome != "command" or delivery.call_slot != "0"):
            raise AdmissionError("runner_response_not_executable")
        if proposal is not None:
            selected = decision.proposal
            if selected.name != proposal["command"] or not strict_equal(selected.argument_data(), proposal["arguments"]):
                raise AdmissionError("runner_response_selection_mismatch")

    def _live(self, delivery, handle, view, *, pending_ticket=None, grant_version=None):
        self._runner_response(delivery, view)
        basis = self._basis(handle, delivery.principal)
        if (basis.task != delivery.task or basis.request_id != delivery.request_id):
            raise AdmissionError("basis_request_mismatch")
        # Authorization policy belongs to the issued basis even if the handler
        # never explicitly read policy_revision through its domain ManagedView.
        if view.policy_revision != basis.cut.identity.policy_revision:
            raise AdmissionError("policy_changed")
        task = view.resolve(delivery.task)
        request = view.resolve(self._request_ref(delivery.request_id))
        grant = view.resolve(delivery.grant)
        tv, rv, gv = task.values, request.values, grant.values
        if (tv["principal"] != delivery.principal or tv["state"] != "active"
                or tv["generation"] != basis.task_generation or tv["active_request"] != delivery.request_id):
            raise AdmissionError("stale_task")
        if (rv["task"] != delivery.task or rv["task_generation"] != basis.task_generation
                or rv["basis_handle"] != handle or rv["basis_kind"] != basis.kind
                or delivery.call_slot not in rv["call_slots"]):
            raise AdmissionError("stale_request")
        if pending_ticket is None:
            if rv["state"] != "open" or rv["invocation_id"] is not None:
                raise AdmissionError("mutation_slot_unavailable")
        elif rv["state"] != "pending_approval" or rv["approval_id"] != pending_ticket:
            raise AdmissionError("approval_request_changed")
        if basis.kind == "program":
            self._require_agent_runtime()
            from ._programs import validate_admission
            try:
                if callable(getattr(view, "version", None)):
                    validate_admission(self, view, delivery, thaw(rv))
                else:
                    with self.coordinator.read() as program_cut:
                        validate_admission(self, program_cut.view, delivery, thaw(rv))
            except (ValueError, LookupError, TypeError):
                raise AdmissionError("program_admission_denied") from None
            if gv["principal"] != delivery.principal or gv["kind"] != "activity" or gv["revoked"]:
                raise AdmissionError("grant_denied")
        elif (gv["principal"] != delivery.principal or gv["task"] != delivery.task
                or gv["task_generation"] != basis.task_generation or gv["revoked"]
                or gv["kind"] != "authority"):
            raise AdmissionError("grant_denied")
        try:
            if callable(getattr(view, "existence_version", None)):
                use = self.grant_authority.check_use(view, delivery.grant, principal=delivery.principal)
            else:
                with self.coordinator.read() as cut:
                    use = self.grant_authority.check_use(cut.view, delivery.grant, principal=delivery.principal)
            if not use.allowed:
                raise AdmissionError("grant_denied")
        except GrantIntegrityError:
            raise AdmissionError("grant_integrity_denied") from None
        if grant_version is not None and grant.version != grant_version:
            raise AdmissionError("grant_changed")
        return basis, grant

    def _resource_schema(self, resource):
        typ = resource.key.entity_type if type(resource) is Ref else (
            resource.owner.entity_type if type(resource) is CollectionRef else None)
        if typ not in self._entities or typ in self._system_types:
            raise AdmissionError("invalid_domain_resource")
        if type(resource) is CollectionRef:
            self._entities[typ].validate_collection_ref(resource)

    def _check_refs(self, value, view):
        """Typed argument/normalized reference liveness, without reading unused fields."""
        if type(value) is Ref:
            self._resource_schema(value)
            if not view.exists(value):
                raise AdmissionError("resource_generation_unavailable")
        elif type(value) is CollectionRef:
            self._resource_schema(value)
            view.resolve_collection(value).members
        elif type(value) in (tuple, list):
            for item in value:
                self._check_refs(item, view)
        elif type(value) is dict:
            for item in value.values():
                self._check_refs(item, view)

    def _domain_raw(self, plan):
        if type(plan) is not Plan or type(plan.effects) is not tuple or type(plan.delegations) is not tuple:
            raise TypeError("One trusted handler must return a typed Plan")
        if len(plan.delegations) > 1 or any(type(item) is not DelegationSpec for item in plan.delegations):
            raise AdmissionError("unsupported_delegation")
        raw = v.patch_data(plan.patch)
        touched = [x[0] for x in raw["creates"]]
        touched += [x[0].key.entity_type for x in raw["updates"]]
        touched += [ref.key.entity_type for ref in raw["deletes"]]
        if any(typ in self._system_types for typ in touched):
            raise AdmissionError("private_system_write")
        effects = []
        for effect in plan.effects:
            if type(effect) is not EffectSpec:
                raise TypeError("Effects require typed immutable specs")
            self._resource_schema(effect.resource)
            if effect.stop_activity is not None:
                self._resource_schema(effect.stop_activity)
            effects.append({"resource": effect.resource, "action": v.text(effect.action),
                            "payload": effect.payload, "stop_activity": effect.stop_activity,
                            "stop_origin": None})
        return raw, v.decode(v.encode(tuple(effects)))

    def _track_writes(self, view, raw):
        for ref, _ in raw["updates"]:
            view.resolve(ref)  # whole-record normalization input dependency
        for ref in raw["deletes"]:
            view.resolve(ref)
        for typ, ident, _, _ in raw["creates"]:
            if any(ref.key.entity_id == ident for ref in view.entities(typ)):
                raise AdmissionError("create_identity_exists")

    @staticmethod
    def _matches(reads, current):
        parsed = v.read_set(reads)
        entries = current.derived_entries
        versions = {key: entry.version for key, entry in entries.items()}
        if any(dep.key not in entries or entries[dep.key].status != "current" for dep in parsed.queries):
            return False
        return parsed.matches(current.view, query_versions=versions)

    def _ordinary_ref_values(self, entry, promotion, *, reader=None, backlink_reads=None):
        values = entry[3]
        if promotion is not None and entry[1] == promotion["activity"]:
            if (entry[0] != "create" or type(values) is not dict
                    or values.get(promotion["grant_field"]) != promotion["anchor"]):
                raise AdmissionError("delegation_denied")
            return {name: value for name, value in values.items() if name != promotion["grant_field"]}
        spec = self.grant_authority.activities.get(entry[1].key.entity_type)
        if spec is not None and entry[0] == "update":
            if reader is None:
                raise AdmissionError("private_system_write")
            old = reader.field(entry[1], spec[2])
            if values[spec[2]] != old:
                raise AdmissionError("private_system_write")
            if old is not None:
                try:
                    proof = self.grant_authority.check_activity_backlink(reader, entry[1], spec[2], old)
                except GrantIntegrityError:
                    raise AdmissionError("grant_integrity_denied") from None
                if proof is None:
                    raise AdmissionError("private_system_write")
                if backlink_reads is not None:
                    data = v.reads_data(proof.read_set)
                    if data not in backlink_reads:
                        backlink_reads.append(data)
                return {name: value for name, value in values.items() if name != spec[2]}
        return values

    def _scope(self, command, raw, normalized, effects, grant, promotion=None, *, reader=None, backlink_reads=None):
        gv = thaw(grant.values)
        profile = self._permission_profiles[command]
        if command not in gv["actions"]:
            raise AdmissionError("command_not_granted")
        allowed = {v.record_identity(item) for item in gv["resources"]}
        created = {v.identity(entry[1]) for entry in normalized if entry[0] == "create"
                   and entry[1].key.entity_type in gv["create_types"]
                   and entry[1].key.entity_type in profile.create_types}
        needed = set()
        for entry in normalized:
            needed.update(v.resources(entry[1]))
            if len(entry) > 3:
                needed.update(v.resources(self._ordinary_ref_values(entry, promotion, reader=reader, backlink_reads=backlink_reads)))
            if entry[0] == "create" and v.identity(entry[1]) not in created:
                raise AdmissionError("creation_not_granted")
        for effect in effects:
            if effect["action"] not in gv["actions"]:
                raise AdmissionError("effect_not_granted")
            if effect["action"] not in profile.effect_actions:
                raise AdmissionError("command_profile_denied")
            needed.update(v.resources(effect["resource"]))
            needed.update(v.resources(effect["payload"]))
            needed.update(v.resources(effect["stop_activity"]))
        if not needed.issubset(allowed | created):
            raise AdmissionError("normalized_scope_not_granted")
        if promotion is not None:
            if not gv["allow_activity_transfer"] or not profile.allow_activity_transfer:
                raise AdmissionError("delegation_denied")
            if not set(needed).issubset(set(promotion["resources"]) | created):
                raise AdmissionError("delegation_denied")
            if not {command, *(effect["action"] for effect in effects)}.issubset(promotion["actions"]):
                raise AdmissionError("delegation_denied")
        return tuple(sorted(needed, key=repr))

    def _prepare_promotion(self, plan, raw, *, plan_id, command, grant):
        if not plan.delegations:
            return None
        spec = plan.delegations[0]
        self._resource_schema(spec.activity)
        metadata = self.grant_authority.activities.get(spec.activity.key.entity_type)
        if metadata is None or not metadata[3] or spec.activity.key.generation != 1:
            raise AdmissionError("delegation_denied")
        creates = [entry for entry in raw["creates"] if entry[:2] ==
                   (spec.activity.key.entity_type, spec.activity.key.entity_id)]
        if len(creates) != 1 or creates[0][3] != "durable":
            raise AdmissionError("delegation_denied")
        state_field, _, grant_field, _ = metadata
        values = creates[0][2]
        if values.get(grant_field) is not None:
            raise AdmissionError("private_system_write")
        if type(spec.resources) is not tuple or type(spec.actions) is not tuple or type(spec.create_types) is not tuple:
            raise TypeError("Delegation requires immutable scope tuples")
        for resource in spec.resources:
            self._resource_schema(resource)
        resources = set().union(*(v.resources(resource) for resource in spec.resources)) if spec.resources else set()
        gv = thaw(grant.values)
        profile = self._permission_profiles[command]
        for items in (spec.actions, spec.create_types):
            if len(set(items)) != len(items) or any(type(x) is not str or not x for x in items):
                raise AdmissionError("delegation_denied")
        if (not gv["allow_activity_transfer"] or not profile.allow_activity_transfer
                or not resources.issubset({v.record_identity(item) for item in gv["resources"]})
                or not set(spec.actions).issubset(gv["actions"])
                or not set(spec.actions).issubset({command, *profile.effect_actions})
                or not set(spec.create_types).issubset(gv["create_types"])
                or not set(spec.create_types).issubset(profile.create_types)):
            raise AdmissionError("delegation_denied")
        anchor = self._ref(self.system.Grant, plan_id + "-anchor")
        values[grant_field] = anchor
        return {"activity": spec.activity, "state_field": state_field, "grant_field": grant_field,
                "anchor": anchor, "resources": tuple(sorted(resources, key=repr)),
                "actions": tuple(sorted(spec.actions)), "create_types": tuple(sorted(spec.create_types))}

    def _seal_authority(self, data, normalized, scope):
        plan_id, delivery = data["plan_id"], data["delivery"]
        operation = self._ref(self.system.Grant, plan_id + "-grant")
        invocation = self._ref(self.system.Invocation, plan_id)
        promotion = data["promotion"]
        header = {"principal": delivery["principal"], "task": delivery["task"],
            "task_generation": data["task_generation"], "owner_activity": None, "kind": "operation",
            "resources": tuple({"entity_type": a, "entity_id": b, "generation": c, "collection_field": d}
                               for a, b, c, d in scope),
            "actions": tuple(sorted({data["proposal"]["command"], *(e["action"] for e in data["effects"])})),
            "create_types": (), "revoked": False, "parent": v.resource_record(delivery["grant"]),
            "plan_id": plan_id, "allow_activity_transfer": False,
            "created_resources": tuple(v.resource_record(entry[1]) for entry in normalized if entry[0] == "create"),
            "source_invocation": v.resource_record(invocation)}
        if data.get("program") is not None:
            header.update(task=None, task_generation=None, owner_activity=data["program"]["activation"])
        anchor_header = None
        if promotion is not None:
            entry = next((entry for entry in normalized if entry[0] == "create"
                          and entry[1] == promotion["activity"]), None)
            if entry is None or entry[3][promotion["state_field"]] not in self.grant_authority.activities[entry[1].key.entity_type][1]:
                raise AdmissionError("delegation_denied")
            header.update(task=None, task_generation=None, owner_activity=v.resource_record(promotion["activity"]),
                          parent=v.resource_record(promotion["anchor"]))
            anchor_header = dict(header, kind="activity", resources=tuple(
                {"entity_type": a, "entity_id": b, "generation": c, "collection_field": d}
                for a, b, c, d in promotion["resources"]), actions=promotion["actions"],
                create_types=promotion["create_types"], parent=v.resource_record(delivery["grant"]),
                created_resources=(), source_invocation=None)
        return {"operation": operation, "invocation": invocation, "operation_header": header,
                "anchor": None if promotion is None else promotion["anchor"], "anchor_header": anchor_header}

    def _pin_stop_origins(self, effects, reader):
        dependencies = []
        for effect in effects:
            activity = effect["stop_activity"]
            if activity is None:
                continue
            spec = self.grant_authority.activities.get(activity.key.entity_type)
            if spec is None:
                raise AdmissionError("delegation_denied")
            anchor = reader.field(activity, spec[2])
            if type(anchor) is not Ref:
                raise AdmissionError("delegation_denied")
            plan = reader.field(anchor, "plan_id")
            if type(plan) is not str or not plan:
                raise AdmissionError("delegation_denied")
            invocation = self._ref(self.system.Invocation, plan)
            operation = reader.field(invocation, "grant")
            check = self.grant_authority.check_activity_occurrence(reader, activity,
                original_anchor=anchor, start_invocation=invocation, start_operation_grant=operation)
            effect["stop_origin"] = {"activity": v.resource_record(activity), "anchor": anchor,
                "start_invocation": invocation, "start_operation_grant": operation}
            dependencies.append(v.reads_data(check.read_set))
        return tuple(dependencies)

    def _footprint_refs(self, value, view, created):
        if type(value) is Ref:
            if value.key not in created:
                self._check_refs(value, view)
        elif type(value) is CollectionRef:
            if value.owner not in created:
                self._check_refs(value, view)
        elif type(value) in (tuple, list):
            for item in value:
                self._footprint_refs(item, view, created)
        elif type(value) is dict:
            for item in value.values():
                self._footprint_refs(item, view, created)

    def _extract(self, expectation, inputs):
        try:
            self._require_index()
            data = v.decode(expectation)
            delivery = self._delivery_data(data)
            _, grant = self._live(delivery, data["proposal"]["basis"], inputs.base.view)
            check_prepare_origin(self,data,inputs.base.view)
            if grant.version != data["grant_version"] or not self._matches(data["reads"], inputs.base):
                raise AdmissionError("conflict")
            normalized, ledgers = v.projection(data["raw"], inputs.base_snapshot, inputs.staged)
            backlink_reads = []
            for entry in normalized:
                if len(entry) > 3:
                    self._check_refs(self._ordinary_ref_values(entry, data["promotion"], reader=inputs.base.view,
                                                              backlink_reads=backlink_reads), inputs.staged)
            for effect in data["effects"]:
                self._check_refs(effect["resource"], inputs.staged)
                self._check_refs(effect["payload"], inputs.staged)
                self._check_refs(effect["stop_activity"], inputs.staged)
            scope = self._scope(data["proposal"]["command"], data["raw"], normalized, data["effects"], grant, data["promotion"],
                                reader=inputs.base.view, backlink_reads=backlink_reads)
            if data.get("program") is not None:
                self._require_agent_runtime()
                from ._programs import public_candidate
                try:
                    public_candidate(self, data, normalized, inputs.base.view)
                except (ValueError, TypeError, LookupError):
                    raise AdmissionError("program_information_flow_denied") from None
            created = {entry[1].key for entry in normalized if entry[0] == "create"}
            with ReadTracker(inputs.base.view) as footprint_reads:
                for entry in normalized:
                    if len(entry) > 3:
                        self._footprint_refs(self._ordinary_ref_values(entry, data["promotion"], reader=inputs.base.view,
                                                                     backlink_reads=backlink_reads), footprint_reads.view, created)
                for effect in data["effects"]:
                    self._footprint_refs(effect["resource"], footprint_reads.view, created)
                    self._footprint_refs(effect["payload"], footprint_reads.view, created)
                    self._footprint_refs(effect["stop_activity"], footprint_reads.view, created)
            authority = self._seal_authority(data, normalized, scope)
            private_ledgers = []
            for ref in (authority["operation"], authority["invocation"], authority["anchor"]):
                if ref is not None:
                    if inputs.base_snapshot.generations.get((ref.key.entity_type, ref.key.entity_id), 0):
                        raise AdmissionError("create_identity_changed")
                    private_ledgers.append(ref)
            candidate = dict(data, normalized=normalized, ledgers=ledgers, scope=scope,
                             authority=authority, private_ledgers=tuple(private_ledgers),
                             backlink_reads=tuple(backlink_reads), footprint_reads=v.reads_data(footprint_reads.read_set))
            candidate["digest"] = v.digest(candidate)
            return PreviewCandidate(v.encode(candidate))
        except AdmissionError as error:
            return RejectedDecision(error.code)

    def _delivery_data(self, data):
        d = data["delivery"]
        # Internal reconstruction is still issued by this live host, never model args.
        return self._new_delivery(d["principal"], d["task"], d["request_id"], d["call_slot"], d["grant"])

    def deliver(self, origin, delivery, proposal):
        key = None
        try:
            self._context(origin, delivery)
            self._authorize_receipt(origin, delivery)
            self._require_index()
            # Canonical RAW delivery data before any new-proposal schema parsing.
            proposal = v.decode(v.encode(proposal))
            fingerprint = v.digest(proposal)
            key = delivery.key
            previous = self._deliveries.get(key) or self._pending.get(key) or self._failures.get(key)
            if previous is not None:
                if previous["fingerprint"] != fingerprint:
                    return CommandResult("collision", error="delivery_fingerprint_collision")
                return self.receipt(origin, delivery)
            if self.coordinator.phase != Phase.IDLE:
                return CommandResult("busy", error="semantic_guarded")
            proposal = self._raw_proposal(proposal)
            with self.coordinator.read() as response_cut:
                self._runner_response(delivery, response_cut.view, proposal)
            registry, descriptor = self._schema(proposal)
            basis = self._basis(proposal["basis"], origin.principal)
            if origin.channel != basis.kind and not (origin.channel == "host" and basis.kind == "program"):
                raise AdmissionError("origin_basis_mismatch")
            self._reserve_failure(delivery, basis.handle)
            plan_id = uuid4().hex
            self._pending[key] = {"principal": origin.principal, "fingerprint": fingerprint}
            prepare_errors = []
            def prepare_domain(current):
                _, grant = self._live(delivery, basis.handle, current)
                with self.coordinator.read() as source_cut:
                    prepare_origin,source_reads=capture_prepare_origin(self,source_cut.view,
                        delivery,basis,descriptor.definition_id)
                # The Registry's compiled strict WIRE validator is the single argument schema.
                try:
                    arguments = registry._definition("command", descriptor.definition_id).validate(
                        proposal["arguments"], wire=True)
                except (TypeError, ValueError) as error:
                    raise AdmissionError("invalid_arguments") from error
                with ReadTracker(basis.cut.view, query=lambda query, tracker:
                                 _result(basis.cut.derived_entries, query, tracker)) as reads:
                    self._check_refs(arguments, reads.view)
                    plan = v.sync(registry.command_handler(descriptor.definition_id)(
                        PrepareContext(reads.view, plan_id, _origin=prepare_origin), **arguments))
                    raw, effects = self._domain_raw(plan)
                    promotion = self._prepare_promotion(plan, raw, plan_id=plan_id,
                        command=descriptor.definition_id, grant=grant)
                    self._track_writes(reads.view, raw)
                stop_reads = self._pin_stop_origins(effects, basis.cut.view)
                if not reads.read_set.complete:
                    raise AdmissionError("incomplete_readset")
                program = None
                if basis.kind == "program":
                    self._require_agent_runtime()
                    from ._programs import validate_admission
                    with self.coordinator.read() as program_cut:
                        admission = program_cut.view.resolve(self._request_ref(delivery.request_id)).values
                        attempt_ref, attempt, run = validate_admission(self, program_cut.view, delivery, thaw(admission))
                    canonical = self._program_candidates.get(basis.handle)
                    if canonical is None:
                        raise AdmissionError("program_result_unavailable")
                    program = {"attempt": attempt_ref, "activation": v.resource_record(run["activation"]),
                        "canonical": canonical.decode("utf-8")}
                data = {
                    "program": program,
                    "prepare_origin": prepare_origin.as_data(), "source_reads": v.reads_data(source_reads),
                    "proposal": proposal, "fingerprint": fingerprint, "plan_id": plan_id,
                    "delivery": {"principal": delivery.principal, "task": delivery.task,
                        "request_id": delivery.request_id, "call_slot": delivery.call_slot, "grant": delivery.grant},
                    "raw": raw, "effects": effects, "promotion": promotion,
                    "task_generation": basis.task_generation, "stop_reads": stop_reads,
                    "reads": v.reads_data(reads.read_set),
                    "argument_resources": tuple(sorted(v.working_resources(arguments), key=repr)),
                    "grant_version": grant.version,
                    "approval_deadline_ns": self._clock() + self._approval_ttl,
                    "provenance": (basis.handle, basis.kind, basis.cut.identity.epoch,
                        basis.cut.identity.sequence, basis.cut.identity.schema_manifest,
                        basis.cut.identity.policy_revision, basis.cut.identity.index_revision),
                }
                return PreparedPreview(v.patch(raw), v.encode(data))
            def prepare(current):
                try:
                    return prepare_domain(current)
                except AdmissionError as error:
                    prepare_errors.append(error.code)
                    raise
            preview = self.coordinator.preview("preview-" + plan_id, prepare, extract=self._extract)
            if preview.status != PreviewStatus.READY:
                self._pending.pop(key, None)
                self._failure_reservations.pop(key, None)
                if preview.status in (PreviewStatus.BUSY, PreviewStatus.RECONCILING):
                    return CommandResult(preview.status.value, error=preview.error)
                code = prepare_errors[0] if prepare_errors else preview.error
                return _rejected(code, default="denied" if prepare_errors else "failed")
            candidate = v.decode(preview.candidate.payload)
            approval = descriptor.metadata.get("approval_required", False)
            if type(approval) is not bool:
                raise AdmissionError("invalid_approval_policy")
            if self._approval_policy is not None:
                decision = v.sync(self._approval_policy(deep_freeze(candidate)))
                if type(decision) is not bool:
                    raise TypeError("Approval policy must return exact bool")
                approval = approval or decision
            if approval and candidate.get("program") is not None:
                raise AdmissionError("program_approval_unsupported")
            return self._commit(candidate, pending=approval)
        except CoordinatorUnavailable:
            if key is not None and not any(item["key"] == key for item in self._transactions.values()):
                self._pending.pop(key, None)
                self._failure_reservations.pop(key, None)
            return CommandResult("reconciling", error="coherent_authority_unavailable")
        except Exception as error:
            if key is not None and not any(item["key"] == key for item in self._transactions.values()):
                self._pending.pop(key, None)
                self._failure_reservations.pop(key, None)
            return (_rejected(error.code, default="denied") if isinstance(error, AdmissionError)
                    else CommandResult("failed", error=type(error).__name__))

    def _candidate_live(self, data, inputs, *, ticket_id=None):
        delivery = self._delivery_data(data)
        self._runner_response(delivery, inputs.base.view, data["proposal"])
        _, grant = self._live(delivery, data["proposal"]["basis"], inputs.base.view,
                              pending_ticket=ticket_id, grant_version=data["grant_version"])
        self._schema(data["proposal"])
        check_prepare_origin(self,data,inputs.base.view)
        if inputs.base.identity.policy_revision != data["provenance"][5]:
            raise AdmissionError("policy_changed")
        if (not self._matches(data["reads"], inputs.base)
                or not self._matches(data["footprint_reads"], inputs.base)):
            raise AdmissionError("conflict")
        if any(not self._matches(reads, inputs.base) for reads in (*data["stop_reads"], *data["backlink_reads"])):
            raise AdmissionError("conflict")
        for ref in data["private_ledgers"]:
            if inputs.base_snapshot.generations.get((ref.key.entity_type, ref.key.entity_id), 0):
                raise AdmissionError("create_identity_changed")
        for typ, ident, generation, partition in data["ledgers"]:
            if (inputs.base_snapshot.generations.get((typ, ident), 0) != generation
                    or inputs.base_snapshot.generation_partitions.get((typ, ident)) != partition):
                raise AdmissionError("create_identity_changed")
        return grant

    def _verify_acceptance(self, expectation, inputs):
        try:
            self._require_index()
            expected = v.decode(expectation)
            data, ticket_id = expected["candidate"], expected["ticket_id"]
            grant = self._candidate_live(data, inputs, ticket_id=ticket_id)
            if expected["mode"] == "application":
                normalized, _ = v.projection(data["raw"], inputs.base_snapshot, inputs.staged)
                if v.digest(normalized) != v.digest(data["normalized"]):
                    raise AdmissionError("approval_candidate_changed")
                for entry in normalized:
                    if len(entry) > 3:
                        self._check_refs(self._ordinary_ref_values(entry, data["promotion"], reader=inputs.base.view), inputs.staged)
                for effect in data["effects"]:
                    self._check_refs(effect["resource"], inputs.staged)
                    self._check_refs(effect["payload"], inputs.staged)
                    self._check_refs(effect["stop_activity"], inputs.staged)
                scope = self._scope(data["proposal"]["command"], data["raw"], normalized, data["effects"], grant,
                                    data["promotion"], reader=inputs.base.view)
                if v.digest(self._seal_authority(data, normalized, scope)) != v.digest(data["authority"]):
                    raise AdmissionError("approval_candidate_changed")
                if ticket_id is not None:
                    ticket = inputs.base.view.resolve(self._ref(self.system.ApprovalTicket, ticket_id))
                    if (ticket.values["expires_ns"] <= self._clock()
                            or ticket.values["issuer_epoch"] != inputs.base.identity.epoch):
                        raise AdmissionError("approval_expired")
                    if ticket.values["state"] != "pending" or ticket.values["digest"] != data["digest"]:
                        raise AdmissionError("approval_ticket_changed")
            if not self._system_matches(expected["system"], inputs):
                raise AdmissionError("system_projection_changed")
            self.grant_authority.validate_transition(inputs.base.view, inputs.staged)
            if data.get("program") is not None and expected["mode"] == "application":
                self._require_agent_runtime()
                from ._programs import verify_application
                try:
                    verify_application(self, data, inputs)
                except (ValueError, LookupError, TypeError):
                    raise AdmissionError("program_successor_proof") from None
            return AcceptedDecision(expected["bundle"])
        except GrantIntegrityError as error:
            return RejectedDecision("grant_integrity_denied", error.code)
        except AdmissionError as error:
            return RejectedDecision(error.code)

    def _assemble_effects(self, data, current, invocation, operation_grant):
        """Allocate trusted queue generations from C1 only; no dispatch or fence claim."""
        creates, updates, admitted, ledgers = [], [], [], {}
        for index, effect in enumerate(data["effects"]):
            identity = v.resource_record(effect["resource"])
            resource_id = v.digest(identity)
            resource_ref = self._ref(self.system.EffectResource, resource_id)
            if resource_id not in ledgers:
                if current.exists(resource_ref):
                    old = thaw(current.resolve(resource_ref).values)
                    if v.digest(old["resource"]) != v.digest(identity):
                        raise AdmissionError("effect_resource_collision")
                    ledgers[resource_id] = [resource_ref, old, False]
                else:
                    ledgers[resource_id] = [resource_ref, {"resource": identity,
                        "desired_generation": 0, "last_fence": 0, "active_effect_id": None,
                        }, True]
            _, state, _ = ledgers[resource_id]
            state["desired_generation"] = v.integer(state["desired_generation"]) + 1
            effect_id = data["plan_id"] + "-effect-" + str(index)
            operation_id = data["plan_id"] + "-operation-" + str(index)
            values = {"invocation": invocation, "grant": operation_grant, "resource": identity,
                "action": effect["action"], "payload": v.envelope("agent-native.effect-payload/1", effect["payload"]),
                "operation_id": operation_id, "desired_generation": state["desired_generation"],
                "state": "pending", "claim_fence": None, "cancel_requested": False, "evidence_ids": (),
                "stop_origin": effect["stop_origin"], "stop_prerequisite": None, "local_no_send_reason": None}
            creates.append(Create(self.system.EffectIntent.__entity_type__, effect_id, values))
            admitted.append(dict(effect, effect_id=effect_id, operation_id=operation_id,
                desired_generation=state["desired_generation"], grant=operation_grant))
        for resource_ref, state, new in ledgers.values():
            if new:
                creates.append(Create(resource_ref.key.entity_type, resource_ref.key.entity_id, state))
            else:
                updates.append(Update(resource_ref, {"desired_generation": state["desired_generation"]}))
        return tuple(creates), tuple(updates), tuple(admitted)

    def _commit(self, data, *, pending=False, approved_by=None):
        self._require_index()
        delivery = self._delivery_data(data)
        self._reserve_failure(delivery, data["proposal"]["basis"])
        key, plan_id = delivery.key, data["plan_id"]
        request = self._request_ref(delivery.request_id)
        ticket = self._ref(self.system.ApprovalTicket, plan_id + "-approval")
        operation_grant = data["authority"]["operation"]
        invocation = data["authority"]["invocation"]
        built, prepare_errors, correlation = [], [], {}
        operation_id = "command-" + plan_id
        def build(current):
            self._require_index()
            # Freshness is checked again by the staged verifier against this same cut.
            self._live(delivery, data["proposal"]["basis"], current,
                pending_ticket=ticket.key.entity_id if approved_by is not None else None,
                grant_version=data["grant_version"])
            if pending:
                system = Patch(creates=(Create(ticket.key.entity_type, ticket.key.entity_id, {
                    "principal": delivery.principal, "task": delivery.task, "request": request,
                    "grant": delivery.grant, "plan_id": plan_id, "digest": data["digest"],
                    "basis_handle": data["proposal"]["basis"],
                    "expires_ns": data["approval_deadline_ns"], "issuer_epoch": data["provenance"][2],
                    "raw_plan": v.envelope(RAW, data),
                    "candidate": v.envelope(CANDIDATE, data["normalized"]),
                    "dependencies": v.envelope(DEPS, data["reads"]),
                    "provenance": v.envelope(PROVENANCE, data["provenance"]),
                    "state": "pending", "approved_by": None}),),
                    updates=(Update(request, {"state": "pending_approval", "approval_id": ticket.key.entity_id}),))
                receipt = {"plan_id": plan_id, "outcome": "approval_required", "digest": data["digest"],
                           "invocation": None, "ticket": ticket, "effects": ()}
                raw = Patch()
            else:
                effect_creates, effect_updates, effects = self._assemble_effects(data, current, invocation, operation_grant)
                updates = [Update(request, {"state": "consumed", "invocation_id": plan_id,
                                            "approval_id": ticket.key.entity_id if approved_by is not None else None})]
                if approved_by is not None:
                    updates.append(Update(ticket, {"state": "consumed", "approved_by": approved_by}))
                task = current.resolve(delivery.task)
                roots = {v.record_identity(item) for item in thaw(task.values["working_resources"])}
                roots.update(data["argument_resources"])
                for entry in data["normalized"]:
                    if entry[0] in ("create", "update"):
                        roots.add(v.identity(entry[1]))
                    elif entry[0] == "delete":
                        roots = {root for root in roots if root[:3] != v.identity(entry[1])[:3]}
                for effect in data["effects"]:
                    roots.update(v.working_resources(effect["resource"]))
                working = tuple({"entity_type": a, "entity_id": b, "generation": c, "collection_field": d}
                                for a, b, c, d in sorted(roots, key=repr))
                updates.append(Update(delivery.task, {"working_resources": working}))
                system = Patch(creates=(
                    Create(operation_grant.key.entity_type, operation_grant.key.entity_id,
                           data["authority"]["operation_header"]),
                    Create(invocation.key.entity_type, invocation.key.entity_id, {
                        "principal": delivery.principal, "task": delivery.task, "request": request,
                        "grant": operation_grant, "plan_id": plan_id, "digest": data["digest"],
                        "delivery_fingerprint": data["fingerprint"],
                        "effect_ids": tuple(e["effect_id"] for e in effects),
                        "approval_id": ticket.key.entity_id if approved_by is not None else None}),
                    ) + (() if data["authority"]["anchor"] is None else (
                        Create(self.system.Grant.__entity_type__, data["authority"]["anchor"].key.entity_id,
                               data["authority"]["anchor_header"]),)) + effect_creates,
                    updates=tuple(updates) + effect_updates)
                receipt = {"plan_id": plan_id, "outcome": "pending_external" if effects else "committed_local",
                           "digest": data["digest"], "invocation": invocation,
                           "ticket": ticket if approved_by is not None else None, "effects": effects}
                raw = v.patch(data["raw"])
            if self.system.supports_agent_runtime:
                from ._conversation import append_conversation_events
                with self.coordinator.read() as event_cut:
                    if not pending and data.get("program") is not None:
                        self._require_agent_runtime()
                        from ._programs import command_patch
                        program_patch = command_patch(self, event_cut.view, data, invocation)
                        system = Patch(system.creates + program_patch.creates,
                            system.updates + program_patch.updates, system.deletes + program_patch.deletes)
                    system = append_conversation_events(self.system, event_cut.view, system)
            system_data = v.patch_data(system)
            bundle = {"protocol": PROTOCOL, "event": {"key": key, "principal": delivery.principal,
                       "fingerprint": data["fingerprint"], "receipt": receipt}}
            expected = {"candidate": data, "ticket_id": ticket.key.entity_id if approved_by is not None else None,
                        "mode": "ticket" if pending else "application", "system": system_data, "bundle": bundle}
            built.append(deep_freeze(bundle))
            combined = Patch(raw.creates + system.creates, raw.updates + system.updates, raw.deletes + system.deletes)
            return PreparedChange(combined, acceptance=StagedAcceptance(v.encode(expected), self._verify_acceptance))
        def prepare(current):
            try:
                return build(current)
            except AdmissionError as error:
                prepare_errors.append(error.code)
                raise
        def on_terminal(terminal):
            try:
                if type(terminal) is not SubmissionResult or terminal.operation_id != operation_id:
                    raise ValueError("Foreign command terminal operation")
                identity = (terminal.transaction_id, terminal.operation_id, terminal.ticket)
                if correlation:
                    if identity != correlation["identity"]:
                        raise ValueError("Uncorrelated command terminal result")
                elif terminal.status != Status.FAILED or terminal.error != "not_submitted":
                    raise ValueError("Terminal notification before accepted request correlation")
                self._terminal(terminal, delivery, built[-1], data["fingerprint"], data["proposal"]["basis"])
                if terminal.status == Status.COMMITTED and data.get("program") is not None:
                    self.release_basis(data["proposal"]["basis"])
            except BaseException:
                self._index_valid = False
                raise
        self._pending[key] = {"principal": delivery.principal, "fingerprint": data["fingerprint"],
                              "basis": data["proposal"]["basis"]}
        result = self.coordinator.submit(operation_id, prepare, memory_only=False,
                                        busy=BusyPolicy.REJECT, on_terminal=on_terminal)
        correlation["identity"] = (result.transaction_id, result.operation_id, result.ticket)
        if result.status in (Status.AWAITING_DB, Status.RECONCILING):
            self._transactions[result.transaction_id] = {"key": key, "bundle": built[-1],
                "operation_id": result.operation_id, "ticket": result.ticket}
            return CommandResult(result.status.value, submission=result)
        self._pending.pop(key, None)
        self._failure_reservations.pop(key, None)
        failure = self._failures.get(key)
        if failure is not None:
            return CommandResult("failed_no_effect", submission=result,
                                 error=failure["error"], new_flow_required=True)
        if result.status == Status.BUSY:
            return CommandResult("busy", submission=result, error=result.error)
        return _rejected(prepare_errors[0] if prepare_errors else result.error,
                         submission=result, default="denied" if prepare_errors else "failed")

    def _committed(self, transaction_id, bundle, key):
        payload = thaw(bundle)
        self._fold(payload["event"], self._deliveries)
        self._known_commits.add(transaction_id)
        self._failures.pop(key, None)
        self._failure_reservations.pop(key, None)
        self._pending.pop(key, None)
        self._transactions.pop(transaction_id, None)

    def _terminal(self, result, delivery, bundle, fingerprint, basis_handle):
        """Only the correlated Coordinator callback is a terminal abort proof.

        Journal absence while SQL is running is never used here. A failed attempt
        requires a new trusted flow; exact duplicates do not replay the handler.
        A protected tombstone survives capability GC/reissue until its basis is
        invalidated. Tracking capacity is reserved before executing new work.
        """
        if result.status == Status.COMMITTED:
            self._committed(result.transaction_id, bundle, delivery.key)
        elif result.status == Status.FAILED:
            if result.error not in ("confirmed_rollback", "not_submitted"):
                raise ValueError("Unsupported command terminal failure")
            self._failures[delivery.key] = {"principal": delivery.principal,
                "fingerprint": fingerprint, "error": result.error, "basis": basis_handle}
            self._failure_reservations.pop(delivery.key, None)
            self._pending.pop(delivery.key, None)
            self._transactions.pop(result.transaction_id, None)
        else:
            raise ValueError("Uncertain outcomes are not terminal notifications")

    def approve(self, origin, ticket, digest):
        """Trusted user decision on one exact ticket, never a model approved flag."""
        try:
            self._auth(origin, host=True)
            with self.coordinator.read() as cut:
                record = cut.view.resolve(ticket)
                if (ticket.key.entity_type != self.system.ApprovalTicket.__entity_type__
                        or record.values["principal"] != origin.principal):
                    raise AdmissionError("approval_denied")
                self._receipt_authorized(origin, record.values["principal"])
                self._require_index()
                if record.values["digest"] != digest:
                    raise AdmissionError("approval_digest_mismatch")
                data = v.open_envelope(thaw(record.values["raw_plan"]), RAW)
                sealed = dict(data)
                stored_digest = sealed.pop("digest")
                if v.digest(sealed) != stored_digest or stored_digest != digest:
                    raise AdmissionError("approval_payload_changed")
                if v.digest(v.open_envelope(thaw(record.values["provenance"]), PROVENANCE)) != v.digest(data["provenance"]):
                    raise AdmissionError("approval_payload_changed")
                if v.digest(v.open_envelope(thaw(record.values["candidate"]), CANDIDATE)) != v.digest(data["normalized"]):
                    raise AdmissionError("approval_payload_changed")
                if v.digest(v.open_envelope(thaw(record.values["dependencies"]), DEPS)) != v.digest(data["reads"]):
                    raise AdmissionError("approval_payload_changed")
            delivery = self._delivery_data(data)
            if delivery.principal != origin.principal:
                raise AdmissionError("approval_denied")
            previous = self._deliveries.get(delivery.key)
            if previous is not None and previous["receipt"]["invocation"] is not None:
                return CommandResult(previous["receipt"]["outcome"], self._receipt(previous))
            failure = self._failures.get(delivery.key)
            if failure is not None:
                return CommandResult("failed_no_effect", error=failure["error"], new_flow_required=True)
            if delivery.key in self._pending:
                return CommandResult("awaiting_db")
            if self.coordinator.phase != Phase.IDLE:
                return CommandResult("busy")
            self._basis(data["proposal"]["basis"], origin.principal)
            if (record.values["expires_ns"] <= self._clock()
                    or record.values["issuer_epoch"] != self._bases[data["proposal"]["basis"]].cut.identity.epoch):
                raise AdmissionError("approval_expired")
            if record.values["state"] != "pending":
                raise AdmissionError("approval_ticket_changed")
            return self._commit(data, approved_by=origin.principal)
        except CoordinatorUnavailable:
            return CommandResult("reconciling", error="coherent_authority_unavailable")
        except Exception as error:
            return (_rejected(error.code, default="denied") if isinstance(error, AdmissionError)
                    else CommandResult("failed", error=type(error).__name__))

    def _fold(self, event, index):
        if type(event) is not dict or set(event) != {"key", "principal", "fingerprint", "receipt"}:
            raise ValueError("Malformed command journal event")
        key = event["key"]
        if type(key) is not tuple or len(key) != 5:
            raise ValueError("Malformed delivery key")
        if (key[0] != self.system.TaskSession.__entity_type__ or type(key[1]) is not str
                or type(key[2]) is not int or key[2] < 1 or any(type(x) is not str or not x for x in key[3:])):
            raise ValueError("Invalid delivery identity")
        for name in ("principal", "fingerprint"):
            v.text(event[name])
        receipt = event["receipt"]
        if type(receipt) is not dict or set(receipt) != {"plan_id", "outcome", "digest", "invocation", "ticket", "effects"}:
            raise ValueError("Malformed receipt")
        v.text(receipt["plan_id"])
        if (type(receipt["digest"]) is not str or len(receipt["digest"]) != 64
                or type(receipt["effects"]) is not tuple):
            raise ValueError("Malformed semantic receipt identity")
        if receipt["outcome"] == "approval_required":
            if receipt["invocation"] is not None or type(receipt["ticket"]) is not Ref or receipt["effects"]:
                raise ValueError("Invalid pending approval receipt")
        elif receipt["outcome"] in ("committed_local", "pending_external"):
            if type(receipt["invocation"]) is not Ref:
                raise ValueError("Accepted receipt requires invocation identity")
            if bool(receipt["effects"]) != (receipt["outcome"] == "pending_external"):
                raise ValueError("Invalid external receipt state")
        else:
            raise ValueError("Unsupported receipt outcome")
        previous = index.get(key)
        if previous is not None:
            if previous["principal"] != event["principal"] or previous["fingerprint"] != event["fingerprint"]:
                raise ValueError("Conflicting durable delivery authority")
            old, new = previous["receipt"], event["receipt"]
            if old["plan_id"] != new["plan_id"] or old["digest"] != new["digest"]:
                raise ValueError("Conflicting accepted plan identity")
            if old["invocation"] is not None and v.digest(previous) != v.digest(event):
                raise ValueError("Duplicate accepted invocation")
        index[key] = v.decode(v.encode(event))

    def refresh_journal(self, journal):
        """Install a verified ordered 011 journal snapshot, never an absent-on-error fallback."""
        candidate, revision, known, proofs = {}, 0, set(), {}
        try:
            from agent_native.persistence.storage import CommittedBundle
            for entry in journal:
                if type(entry) is not CommittedBundle or entry.format != "agent-native.kernel-bundle/1":
                    raise ValueError("Use verified typed 011 committed bundles")
                if type(entry.durable_revision) is not int or entry.durable_revision <= revision:
                    raise ValueError("Journal must be in strictly increasing durable revision order")
                revision = entry.durable_revision
                proofs[entry.transaction_id] = entry
                payload = thaw(entry.payload)
                if payload is None:
                    continue
                if type(payload) is dict and payload.get("protocol") == PROTOCOL:
                    if set(payload) != {"protocol", "event"}:
                        raise ValueError("Unknown command journal fields")
                    self._fold(payload["event"], candidate)
                    known.add(entry.transaction_id)
                elif type(payload) is dict and str(payload.get("protocol", "")).startswith("agent-native.commands/"):
                    raise ValueError("Unsupported command journal format")
            if not self._known_commits.issubset(known):
                raise ValueError("Journal omits a known committed command")
            for txid, pending in self._transactions.items():
                if txid in proofs and v.digest(thaw(proofs[txid].payload)) != v.digest(thaw(pending["bundle"])):
                    raise ValueError("Committed journal disagrees with retained command")
            if revision < self._journal_revision:
                raise ValueError("Stale journal proof")
        except BaseException:
            self._index_valid = False
            raise
        self._deliveries, self._journal_revision, self._index_valid = candidate, revision, True
        self._known_commits = known
        for txid in tuple(self._transactions):
            if txid in proofs:
                pending = self._transactions.pop(txid)
                self._pending.pop(pending["key"], None)
                self._failures.pop(pending["key"], None)
                self._failure_reservations.pop(pending["key"], None)

    def _published(self, cut):
        if self._closed:
            return
        txid = self.coordinator.pending_transaction_id
        pending = self._transactions.get(txid)
        if pending is None:
            return
        try:
            self._committed(txid, pending["bundle"], pending["key"])
        except BaseException:
            self._index_valid = False
            raise

    def close(self):
        if self._runner_bridge is not None:
            self._runner_bridge.close()
        for handle in tuple(self._bases):
            self.release_basis(handle)
        self._closed = True
        self._origins.clear()
        self._delivery_capabilities.clear()
        self._failures.clear()
        self._failure_reservations.clear()
