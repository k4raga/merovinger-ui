"""Shared private SYS7 declarations. Register before StateStore construction.

SYS6 accepted databases fail closed; no input migration, reset or second store.
Nested records are closed provenance/decision data, never live capabilities.
"""
from dataclasses import dataclass
from typing import Annotated, Literal
from pydantic import Field
from agent_native.schema import Registry, Ref

U63 = Annotated[int, Field(ge=0, le=2**63-1)]
Positive = Annotated[int, Field(ge=1, le=2**63-1)]
Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


@dataclass(frozen=True, slots=True)
class UiSystemDefinitions:
    registry: Registry
    ResourceIdentity: type
    ProtocolPayload: type
    TransportOutput: type
    TaskSession: type
    Grant: type
    RequestAdmission: type
    ApprovalTicket: type
    Invocation: type
    EffectResource: type
    EffectIntent: type
    EffectEvidence: type
    EffectDispatch: type
    SchemaIdentity: type
    NoEffectActivityRuleRecord: type
    OutputBindingIdentity: type
    RemoteObjectIdentity: type
    SourceIdentity: type
    AdapterWitness: type
    OriginalActivityProof: type
    TargetClaim: type
    StopSendPrerequisite: type
    EffectClaimWitness: type
    EffectCompletionWitness: type
    IncomingEventIdentity: type
    EffectFactLink: type
    OperationCausalClaim: type
    EffectResultWitness: type
    RemoteObjectClaim: type
    SourceObservationWitness: type
    ExternalEventEnvelope: type
    CursorKey: type
    LocalObservationClaim: type
    NoEffectActivityOrigin: type
    FactAdmissionStamp: type
    ExternalFact: type
    OperationResolution: type
    RemoteBindingProof: type
    FieldPermission: type
    ObservedAdvance: type
    TerminalActivityProof: type
    NoEffectLocalEligibility: type
    NoEffectActivityProof: type
    RecordEnvelope: type
    FactApplication: type
    ObservationCursor: type
    LocalNoSendOrigin: type
    LocalNoSendProof: type
    LocalActivityCancellation: type
    LocalCancellationDeclaration: type
    LocalEffectCancellation: type

    @property
    def profile(self):
        return "ui"

    @property
    def supports_agent_runtime(self):
        return False

    @property
    def supports_extensions(self):
        return False


@dataclass(frozen=True, slots=True)
class SystemDefinitions(UiSystemDefinitions):
    BudgetAmounts: type
    RunnerLimits: type
    RunnerUsage: type
    RunnerSession: type
    ModelRequest: type
    AcceptedModelResponse: type
    HistoryAmounts: type
    LineageCut: type
    LineageRoute: type
    LineageLeaf: type
    SkillResourcePin: type
    SkillPin: type
    Conversation: type
    ConversationEvent: type
    RequestInputLineage: type
    SkillControlDelivery: type
    RuntimeStorageReservation: type
    CompactionAttempt: type
    ConversationSummary: type
    ProgramProfileIdentity: type
    ProgramInputContract: type
    ProgramOutputContract: type
    ProgramProvenanceAssociation: type
    ProgramCapabilityCeiling: type
    ProgramArtifactVersion: type
    ExtensionDefinition: type
    ExtensionActivation: type
    ProgramRun: type
    ProgramAttempt: type
    ProgramStep: type
    SavedAction: type
    SavedRule: type

    @property
    def profile(self):
        return "full"

    @property
    def supports_agent_runtime(self):
        return True

    @property
    def supports_extensions(self):
        return True


def system_definitions(namespace="engine.system"):
    """Full SYS7 profile; its accepted schema remains unchanged."""
    return _definitions(namespace, full=True)


def ui_system_definitions(namespace="engine.ui_system"):
    """UI-only built-in C1/C2 profile. Never interprets a full SYS7 database."""
    if namespace == "engine.system":
        raise ValueError("UI profile requires a distinct schema identity")
    return _definitions(namespace, full=False)


def _definitions(namespace, *, full):
    registry = Registry(namespace, 7 if full else 1)

    @registry.record("resource_identity")
    class ResourceIdentity:
        entity_type: str
        entity_id: str
        generation: Positive
        collection_field: str | None = None

    @registry.record("protocol_payload")
    class ProtocolPayload:
        format: str
        payload: str
        sha256: Digest

    @registry.record("transport_output")
    class TransportOutput:
        payload: ProtocolPayload

    @registry.entity("task_session")
    class TaskSession:
        principal: str
        generation: Positive
        state: Literal["active", "completed", "cancelled"]
        active_request: str | None = None
        working_resources: tuple[ResourceIdentity, ...] = ()

    @registry.entity("grant", revision=2)
    class Grant:
        principal: str
        task: Ref[TaskSession] | None
        task_generation: Positive | None
        owner_activity: ResourceIdentity | None
        kind: Literal["authority", "operation", "activity"]
        resources: tuple[ResourceIdentity, ...]
        actions: tuple[str, ...]
        create_types: tuple[str, ...] = ()
        revoked: bool = False
        parent: ResourceIdentity | None = None
        plan_id: str | None = None
        allow_activity_transfer: bool = False
        created_resources: tuple[ResourceIdentity, ...] = ()
        source_invocation: ResourceIdentity | None = None

    @registry.entity("request_admission")
    class RequestAdmission:
        task: Ref[TaskSession]
        task_generation: Positive
        request_id: str
        basis_handle: str
        basis_kind: Literal["agent", "ui", "program"]
        program_attempt: ResourceIdentity | None
        call_slots: tuple[str, ...]
        state: Literal["open", "pending_approval", "consumed", "closed"] = "open"
        approval_id: str | None = None
        invocation_id: str | None = None

    @registry.entity("approval_ticket")
    class ApprovalTicket:
        principal: str
        task: Ref[TaskSession]
        request: Ref[RequestAdmission]
        grant: Ref[Grant]
        plan_id: str
        digest: str
        basis_handle: str
        expires_ns: int
        issuer_epoch: str
        raw_plan: ProtocolPayload
        candidate: ProtocolPayload
        dependencies: ProtocolPayload
        provenance: ProtocolPayload
        state: Literal["pending", "consumed", "declined", "expired"] = "pending"
        approved_by: str | None = None

    @registry.entity("invocation")
    class Invocation:
        principal: str
        task: Ref[TaskSession]
        request: Ref[RequestAdmission]
        grant: Ref[Grant]
        plan_id: str
        digest: str
        delivery_fingerprint: str
        effect_ids: tuple[str, ...]
        approval_id: str | None = None

    @registry.entity("effect_resource")
    class EffectResource:
        resource: ResourceIdentity
        desired_generation: U63 = 0
        last_fence: U63 = 0
        active_effect_id: str | None = None

    @registry.record("schema_identity")
    class SchemaIdentity:
        namespace: str
        registry_revision: Positive
        definition_id: str
        definition_revision: Positive
        fingerprint: Digest

    @registry.record("no_effect_activity_rule")
    class NoEffectActivityRuleRecord:
        slot: str
        creation_pending_states: tuple[str, ...]
        terminal_state: str
        pointer_slot: str | None = None
        pointer_field: str | None = None
        pointer_binding_fields: tuple[str, ...] = ()

    @registry.record("output_binding_identity")
    class OutputBindingIdentity:
        binding_id: str
        binding_revision: Positive
        updater_id: str
        updater_revision: Positive
        action_or_topic: str
        output_schema: SchemaIdentity
        no_effect_activity_rule: NoEffectActivityRuleRecord | None = None

    @registry.record("remote_object_identity")
    class RemoteObjectIdentity:
        remote_identity: str
        identity_epoch: str
        object_kind: str
        object_id: str

    @registry.record("source_identity")
    class SourceIdentity:
        adapter_id: str
        remote_identity: str
        source: str
        source_epoch: str

    @registry.record("adapter_witness")
    class AdapterWitness:
        adapter_id: str
        actions: tuple[str, ...]
        proven_idempotency: bool
        terminal_dedup: bool
        terminal_reconciliation: bool
        remote_identity: str
        capability_version: str
        source: str | None
        source_epoch: str | None
        stop_targeting: Literal["none", "occurrence_fenced", "global_exclusive"] = "none"
        remote_exclusive: bool = False

    @registry.record("original_activity_proof")
    class OriginalActivityProof:
        activity: ResourceIdentity
        anchor: Ref[Grant]
        start_invocation: Ref[Invocation]
        start_operation_grant: Ref[Grant]

    @registry.record("target_claim")
    class TargetClaim:
        slot: str
        target: ResourceIdentity | None
        binding_digest: Digest | None
        remote_object: RemoteObjectIdentity | None

    @registry.record("stop_send_prerequisite")
    class StopSendPrerequisite:
        original: OriginalActivityProof
        resource: ResourceIdentity
        target: TargetClaim
        binding_fields: tuple[str, ...]
        adapter: AdapterWitness
        targeting: Literal["occurrence_fenced", "global_exclusive"]
        remote_fence: str | None

    @registry.entity("effect_intent")
    class EffectIntent:
        invocation: Ref[Invocation]
        grant: Ref[Grant]
        resource: ResourceIdentity
        action: str
        payload: ProtocolPayload
        operation_id: str
        desired_generation: Positive
        state: Literal["pending", "claimed", "confirmed", "failed_no_effect", "unknown", "cancelled_before_effect"] = "pending"
        claim_fence: Positive | None = None
        cancel_requested: bool = False
        evidence_ids: tuple[str, ...] = ()
        stop_origin: OriginalActivityProof | None = None
        stop_prerequisite: StopSendPrerequisite | None = None
        local_no_send_reason: Literal["terminal_occurrence", "absent_occurrence", "binding_changed", "unsupported_stop"] | None = None

    @registry.entity("effect_evidence")
    class EffectEvidence:
        effect: Ref[EffectIntent]
        operation_id: str
        desired_generation: Positive
        claim_fence: Positive
        kind: Literal["confirmed", "failed_no_effect", "uncertain"]
        payload: ProtocolPayload
        source: str | None = None
        source_sequence: U63 | None = None

    @registry.record("effect_claim_witness")
    class EffectClaimWitness:
        capability: AdapterWitness
        capability_digest: Digest
        payload_digest: Digest
        resource: ResourceIdentity
        stop_prerequisite: StopSendPrerequisite | None
        prior_state: Literal["pending", "unknown"]

    @registry.record("effect_completion_witness")
    class EffectCompletionWitness:
        fact: ResourceIdentity
        proof: Literal["rpc_terminal", "operation_terminal", "remote_fence", "readback_only"]
        mode: Literal["execute", "reconcile"]
        resource: ResourceIdentity

    @registry.record("incoming_event_identity")
    class IncomingEventIdentity:
        variant: Literal["effect", "observation"]
        dispatch: ResourceIdentity | None
        adapter_id: str
        remote_identity: str
        source: str | None
        source_epoch: str | None
        ordering_group: str | None
        topic: str
        event_id: str
        operation_id: str | None
        claim_fence: Positive | None
        mode: Literal["execute", "reconcile"] | None

    @registry.record("effect_fact_link")
    class EffectFactLink:
        dispatch: ResourceIdentity
        effect: Ref[EffectIntent]
        invocation: Ref[Invocation]
        operation_grant: Ref[Grant]
        operation_id: str
        resource: ResourceIdentity
        desired_generation: Positive
        claim_fence: Positive
        mode: Literal["execute", "reconcile"]
        effect_action: str
        request_action: str
        input_digest: Digest
        claim_witness: Ref[EffectEvidence]
        claim_witness_digest: Digest
        claim_capability_digest: Digest

    @registry.record("operation_causal_claim")
    class OperationCausalClaim:
        operation_id: str
        desired_generation: Positive

    @registry.record("effect_result_witness")
    class EffectResultWitness:
        evidence_kind: Literal["confirmed", "failed_no_effect", "uncertain"]
        proof: Literal["rpc_terminal", "operation_terminal", "remote_fence", "readback_only"]
        observed_generation_claim: U63 | None
        causal_claim: OperationCausalClaim | None

    @registry.record("remote_object_claim")
    class RemoteObjectClaim:
        name: str
        remote_object: RemoteObjectIdentity

    @registry.record("source_observation_witness")
    class SourceObservationWitness:
        source_sequence: U63 | None
        proof: Literal["event", "snapshot_baseline", "readback"]
        remote_subject: RemoteObjectIdentity
        objects: tuple[RemoteObjectClaim, ...]

    @registry.record("external_event_envelope")
    class ExternalEventEnvelope:
        identity: IncomingEventIdentity
        adapter: AdapterWitness
        producer_schema: SchemaIdentity
        effect_link: EffectFactLink | None
        effect_result: EffectResultWitness | None
        observation: SourceObservationWitness | None
        original_payload: ProtocolPayload

    @registry.record("cursor_key")
    class CursorKey:
        owner: ResourceIdentity
        remote_subject: RemoteObjectIdentity
        owner_binding_digest: Digest
        ordering_group: str

    @registry.record("local_observation_claim")
    class LocalObservationClaim:
        cursor_key: CursorKey
        selection_generation: U63

    @registry.record("no_effect_activity_origin")
    class NoEffectActivityOrigin:
        original: OriginalActivityProof
        start_effect: Ref[EffectIntent]
        target_slot: str
        resource: ResourceIdentity

    @registry.record("fact_admission_stamp")
    class FactAdmissionStamp:
        output_binding: OutputBindingIdentity
        targets: tuple[TargetClaim, ...]
        observation: LocalObservationClaim | None
        activity_origin: OriginalActivityProof | None
        no_effect_origin: NoEffectActivityOrigin | None = None

    @registry.entity("external_fact")
    class ExternalFact:
        external: ExternalEventEnvelope
        external_sha256: Digest
        admission: FactAdmissionStamp
        envelope_sha256: Digest

    @registry.entity("effect_dispatch")
    class EffectDispatch:
        link: EffectFactLink
        adapter: AdapterWitness
        producer_schema: SchemaIdentity
        ordering_group: str | None
        admission: FactAdmissionStamp
        conversation: ResourceIdentity | None
        group_id: str | None
        input_lineage: ResourceIdentity | None
        readback_grant: Ref[Grant] | None
        profile: Literal["agent-native.effect-result/2"]
        static_bytes: U63
        retain_bytes: U63
        apply_bytes: U63
        state: Literal["reserved", "retained", "applied", "not_sent"]
        first_fact: ResourceIdentity | None

    @registry.record("operation_resolution")
    class OperationResolution:
        fact: Ref[ExternalFact]
        effect: Ref[EffectIntent]
        evidence: Ref[EffectEvidence]
        operation_id: str
        claim_fence: Positive
        desired_generation: Positive
        proof: Literal["rpc_terminal", "operation_terminal", "remote_fence", "readback_only"]
        current_attempt: bool
        state_before: Literal["pending", "claimed", "confirmed", "failed_no_effect", "unknown", "cancelled_before_effect"]
        state_after: Literal["pending", "claimed", "confirmed", "failed_no_effect", "unknown", "cancelled_before_effect"]

    @registry.record("remote_binding_proof")
    class RemoteBindingProof:
        fact: Ref[ExternalFact]
        slot: str
        target: ResourceIdentity
        remote_object: RemoteObjectIdentity
        prior_binding_digest: Digest
        binding_fields: tuple[str, ...]
        effect: Ref[EffectIntent]
        operation_id: str

    @registry.record("field_permission")
    class FieldPermission:
        slot: str
        target: ResourceIdentity
        fields: tuple[str, ...]

    @registry.record("observed_advance")
    class ObservedAdvance:
        fact: Ref[ExternalFact]
        cursor_key: CursorKey
        source: SourceIdentity
        selection_generation: U63
        sequence_before: U63 | None
        sequence_after: U63
        proof: Literal["event", "snapshot_baseline", "readback"]
        fields: tuple[FieldPermission, ...]
        causal_effect: Ref[EffectIntent] | None
        causal_generation: U63 | None

    @registry.record("terminal_activity_proof")
    class TerminalActivityProof:
        fact: Ref[ExternalFact]
        original: OriginalActivityProof
        stop_effect: Ref[EffectIntent]
        stop_operation_id: str
        stop_resource: ResourceIdentity
        stop_generation: Positive
        remote_occurrence: RemoteObjectIdentity
        basis: Literal["terminal_stop_result", "fenced_stop_result"]
        remote_fence: str | None
        terminal_state: str

    @registry.record("no_effect_local_eligibility")
    class NoEffectLocalEligibility:
        activity: ResourceIdentity
        existence_before: Literal["present", "absent"]
        state_before: str | None
        backlink_before: Ref[Grant] | None
        binding_fields: tuple[str, ...]
        binding_values_before: tuple[str | None, ...]
        binding_digest_before: Digest | None
        helper_disposition: Literal["ended", "already_terminal", "historical_anchor_only"]

    @registry.record("no_effect_activity_proof")
    class NoEffectActivityProof:
        fact: Ref[ExternalFact]
        original: OriginalActivityProof
        start_effect: Ref[EffectIntent]
        operation_id: str
        resource: ResourceIdentity
        desired_generation: Positive
        claim_fence: Positive
        claim_witness: Ref[EffectEvidence]
        claim_witness_digest: Digest
        basis: Literal["initial_execute_no_effect", "operation_terminal_no_effect"]
        terminal_state: str
        disposition: Literal["ended", "already_terminal", "historical_anchor_only"]
        pointer_target: ResourceIdentity | None
        pointer_cleared: bool
        local_eligibility: NoEffectLocalEligibility

    @registry.record("record_envelope")
    class RecordEnvelope:
        schema_identity: SchemaIdentity
        codec_format: Literal["agent-native.schema/1"]
        record_json: str
        sha256: Digest

    @registry.entity("fact_application")
    class FactApplication:
        fact: Ref[ExternalFact]
        original_envelope_sha256: Digest
        output_binding: OutputBindingIdentity
        outcome: Literal["accepted", "stale", "historical", "rejected"]
        reason: Literal["none", "unsupported_output", "invalid_output", "old_attempt", "old_sequence", "sequence_conflict", "source_unselected", "source_unready", "unordered_source", "unmapped_target", "target_missing", "binding_changed", "terminal_not_proven", "no_effect_unproven", "no_effect_ineligible"]
        reason_detail: str | None
        accepted_output: RecordEnvelope | None
        operation: OperationResolution | None
        bindings: tuple[RemoteBindingProof, ...]
        observation_status: Literal["absent", "advanced", "stale", "unselected", "unmapped", "conflict", "historical", "unordered"]
        observation: ObservedAdvance | None
        terminal: TerminalActivityProof | None
        no_effect_activity: NoEffectActivityProof | None = None

    @registry.entity("observation_cursor")
    class ObservationCursor:
        key: CursorKey
        selected_source: SourceIdentity
        selection_generation: U63
        state: Literal["awaiting_resync", "ready"]
        sequence: U63 | None
        last_application: Ref[FactApplication] | None

    @registry.record("local_no_send_origin")
    class LocalNoSendOrigin:
        effect: Ref[EffectIntent]
        invocation: Ref[Invocation]
        source_request: Ref[RequestAdmission]
        task: Ref[TaskSession]
        task_generation: Positive
        operation_id: str
        resource: ResourceIdentity
        desired_generation: Positive
        input_digest: Digest

    @registry.record("local_no_send_proof")
    class LocalNoSendProof:
        origin: LocalNoSendOrigin
        basis: Literal["pending_no_claim", "first_ready_unconsumed"]
        state_before: Literal["pending", "claimed"]
        claim_witness: Ref[EffectEvidence] | None
        claim_fence: Positive | None
        claim_witness_digest: Digest | None
        issuer_epoch: str
        slot_id: str | None
        slot_generation: Positive | None

    @registry.record("local_activity_cancellation")
    class LocalActivityCancellation:
        original: OriginalActivityProof
        local_eligibility: NoEffectLocalEligibility
        terminal_state: str
        disposition: Literal["ended", "already_terminal", "historical_anchor_only"]
        pointer_target: ResourceIdentity | None
        pointer_cleared: bool

    @registry.entity("local_cancellation_declaration")
    class LocalCancellationDeclaration:
        effect: Ref[EffectIntent]
        output_binding: OutputBindingIdentity
        targets: tuple[TargetClaim, ...]
        activity_binding_fields: tuple[str, ...]

    @registry.entity("local_effect_cancellation")
    class LocalEffectCancellation:
        effect: Ref[EffectIntent]
        proof: LocalNoSendProof
        declaration: Ref[LocalCancellationDeclaration]
        activity: LocalActivityCancellation | None
        activity_status: Literal["applied", "not_declared", "not_new", "multiple_effects", "conflict"]
        reason: Literal["standalone_cancel", "task_cancel"]

    if not full:
        declared = locals()
        return UiSystemDefinitions(**{name: declared[name] for name in UiSystemDefinitions.__dataclass_fields__})

    from ._runtime_definitions import runtime_root_definitions
    roots = runtime_root_definitions(registry, ResourceIdentity=ResourceIdentity,
        SchemaIdentity=SchemaIdentity, TaskSession=TaskSession, RequestAdmission=RequestAdmission)
    from agent_native.runner.definitions import runner_definitions
    runner = runner_definitions(registry, ResourceIdentity=ResourceIdentity, ProtocolPayload=ProtocolPayload,
        TaskSession=TaskSession, Grant=Grant, RequestAdmission=RequestAdmission,
        Invocation=Invocation, ApprovalTicket=ApprovalTicket,
        Conversation=roots.Conversation, RequestInputLineage=roots.RequestInputLineage)
    from agent_native.programming.definitions import programming_definitions
    programs = programming_definitions(registry, ResourceIdentity=ResourceIdentity,
        SchemaIdentity=SchemaIdentity, Grant=Grant)
    from agent_native.runner.memory.definitions import memory_definitions
    memory = memory_definitions(registry, Conversation=roots.Conversation,
        ResourceIdentity=ResourceIdentity, ProtocolPayload=ProtocolPayload,
        ModelRequest=runner.ModelRequest, AcceptedModelResponse=runner.AcceptedModelResponse)
    from agent_native.programming.runtime_definitions import program_runtime_definitions
    runtime = program_runtime_definitions(registry, ResourceIdentity=ResourceIdentity,
        ProtocolPayload=ProtocolPayload, TaskSession=TaskSession, RequestAdmission=RequestAdmission,
        Invocation=Invocation, ModelRequest=runner.ModelRequest,
        AcceptedModelResponse=runner.AcceptedModelResponse, RequestInputLineage=roots.RequestInputLineage,
        ExtensionActivation=programs.ExtensionActivation, ProgramArtifactVersion=programs.ProgramArtifactVersion)
    from agent_native.actions.definitions import action_definitions
    actions = action_definitions(registry, ExtensionActivation=programs.ExtensionActivation, ProtocolPayload=ProtocolPayload,
        RequestInputLineage=roots.RequestInputLineage)
    from agent_native.rules.definitions import rule_definitions
    rules = rule_definitions(registry, ExtensionActivation=programs.ExtensionActivation,
        SavedAction=actions.SavedAction, TaskSession=TaskSession, ProgramRun=runtime.ProgramRun, ProtocolPayload=ProtocolPayload,
        RequestInputLineage=roots.RequestInputLineage)
    declared = locals().copy()
    for group in (roots, runner, programs, memory, runtime, actions, rules):
        declared.update({name: getattr(group, name) for name in group.__dataclass_fields__ if name != "registry"})
    return SystemDefinitions(**{name: declared[name] for name in SystemDefinitions.__dataclass_fields__})
