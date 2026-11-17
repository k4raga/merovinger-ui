"""Callback-free operation resolution from original accepted SYS3 witnesses.

A failed retry RPC does not negate an earlier possibly consumed request. This
module imports no effects types and grants no domain/lifecycle permissions.
"""
from agent_native.schema import EntityKey, Ref
from agent_native.state import ReadView, StagedReadView, strict_equal, thaw
from agent_native.state.tracking import ManagedView
from . import _command_values as v


class OperationProofError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _require(condition, code):
    if not condition:
        raise OperationProofError(code)


def _operation_decision(reader, definitions, external, *, state_before: str,
                            current_attempt: bool, evidence_prefix: tuple[str, ...]):
    """Resolve SAME original result for runtime and callback-free restore.

    Prefix is the exact effect evidence_ids BEFORE this completion, excluding
    future accepted history. Read tracking belongs to the supplied pinned reader.
    First RPC no-effect permits the sole initial pending claim, even after local
    transport loss. Accepted uncertainty/retry requires terminal operation-key proof.
    """
    if type(reader) not in (ReadView, StagedReadView, ManagedView):
        raise TypeError("Use a pinned C1 reader")
    if type(evidence_prefix) is not tuple or type(current_attempt) is not bool:
        raise TypeError("Use an exact immutable history prefix/current flag")
    link, result = external["effect_link"], external["effect_result"]
    _require(link is not None and result is not None, "operation_result_required")
    _require(len(set(evidence_prefix)) == len(evidence_prefix), "duplicate_operation_evidence")
    claims = []
    for identifier in evidence_prefix:
        _require(type(identifier) is str and bool(identifier), "invalid_operation_evidence")
        ref = Ref(EntityKey(definitions.EffectEvidence.__entity_type__, identifier, 1))
        _require(reader.exists(ref), "missing_operation_evidence")
        item = thaw(reader.resolve(ref).values)
        _require(item["effect"] == link["effect"] and item["operation_id"] == link["operation_id"]
                 and item["desired_generation"] == link["desired_generation"], "operation_history_link")
        payload = item["payload"]
        if payload["format"] == "agent-native.effect-claim/2":
            try:
                witness = v.open_envelope(payload, "agent-native.effect-claim/2")
                definitions.registry.check_accepted_record(definitions.EffectClaimWitness.__record_type__, witness)
            except (TypeError, ValueError, UnicodeError):
                raise OperationProofError("invalid_operation_claim") from None
            _require(witness["capability_digest"] == v.digest(witness["capability"])
                     and witness["payload_digest"] == link["input_digest"]
                     and witness["resource"] == link["resource"], "operation_claim_scope")
            _require(not claims or item["claim_fence"] > claims[-1][1], "operation_claim_order")
            claims.append((ref, item["claim_fence"], witness))
        else:
            _require(payload["format"] == "agent-native.effect-evidence/2", "operation_history_format")
    _require(bool(claims), "missing_operation_claim")
    _require(any(claim[0] == link["claim_witness"] and claim[1] == link["claim_fence"] for claim in claims),
             "operation_original_claim_missing")
    latest = claims[-1]
    exact_current = (latest[0] == link["claim_witness"] and latest[1] == link["claim_fence"])
    _require(current_attempt == exact_current, "operation_current_attempt_mismatch")
    if state_before in ("confirmed", "failed_no_effect", "cancelled_before_effect") or not current_attempt:
        return state_before, None
    kind, proof, mode = result["evidence_kind"], result["proof"], link["mode"]
    if kind != "failed_no_effect":
        # Existing positive and uncertainty semantics are deliberately preserved.
        terminal = proof != "readback_only" and (mode == "execute" or proof in ("operation_terminal", "remote_fence"))
        return (kind if terminal and kind != "uncertain" else ("unknown" if mode == "execute" else state_before)), None
    initial_rpc = (mode == "execute" and proof == "rpc_terminal" and state_before in ("claimed", "unknown")
        and evidence_prefix == (link["claim_witness"].key.entity_id,)
        and latest[2]["prior_state"] == "pending")
    original, adapter = claims[0][2]["capability"], external["adapter"]
    same_capability = all(strict_equal(claim[2]["capability"], original) for claim in claims)
    same_namespace = all(adapter[name] == original[name] for name in
                         ("remote_identity", "capability_version"))
    original_history = (claims[0][2]["prior_state"] == "pending"
                        and all(claim[2]["prior_state"] == "unknown" for claim in claims[1:]))
    whole_operation = (proof == "operation_terminal" and state_before in ("claimed", "unknown")
        and same_capability and same_namespace and original_history
        and original["terminal_dedup"]
        and (len(claims) == 1 or original["proven_idempotency"])
        and (mode == "reconcile" or adapter["terminal_dedup"]
             and (len(claims) == 1 or adapter["proven_idempotency"]))
        and (mode != "reconcile" or original["terminal_reconciliation"] and adapter["terminal_reconciliation"]))
    if initial_rpc or whole_operation:
        return "failed_no_effect", ("initial_execute_no_effect" if initial_rpc else "operation_terminal_no_effect")
    # Readback/fence/last retry RPC alone does not prove prior creation absent.
    return ("unknown" if mode == "execute" else state_before), None


def resolve_operation_state(reader, definitions, external, *, state_before: str,
                            current_attempt: bool, evidence_prefix: tuple[str, ...]) -> str:
    """Generic SYS3-compatible result; terminal history never fabricates proof."""
    return _operation_decision(reader, definitions, external, state_before=state_before,
        current_attempt=current_attempt, evidence_prefix=evidence_prefix)[0]


def no_effect_basis(reader, definitions, external, *, state_before: str,
                    current_attempt: bool, evidence_prefix: tuple[str, ...]):
    """Fresh original negative basis, never a preserved terminal state.

    Deliberately None for before-terminal operations, even for a later strong
    result: reinterpretation of an already resolved operation is outside scope.
    """
    return _operation_decision(reader, definitions, external, state_before=state_before,
        current_attempt=current_attempt, evidence_prefix=evidence_prefix)[1]


def has_creation_contradiction(reader, definitions, *, activity: Ref, start_effect: Ref,
                               operation_id: str, evidence_prefix: tuple[str, ...]) -> bool:
    """Earlier genuine positive creation/binding, never raw query success.

    The caller supplies the same exact historical prefix used for operation
    resolution. Prior records are immutable accepted data; current mutable
    Activity fields and future completions are not historical selectors.
    """
    if type(reader) not in (ReadView, StagedReadView, ManagedView) or type(evidence_prefix) is not tuple:
        raise TypeError("Use a pinned C1 reader and immutable history prefix")
    def values(ref):
        _require(reader.exists(ref), "missing_creation_history")
        return thaw(reader.resolve(ref).values)
    for position, identifier in enumerate(evidence_prefix):
        evidence_ref = Ref(EntityKey(definitions.EffectEvidence.__entity_type__, identifier, 1))
        evidence = values(evidence_ref)
        _require(evidence["effect"] == start_effect and evidence["operation_id"] == operation_id,
                 "creation_history_link")
        envelope = evidence["payload"]
        if envelope["format"] == "agent-native.effect-claim/2":
            continue
        _require(envelope["format"] == "agent-native.effect-evidence/2", "creation_history_format")
        completion = v.open_envelope(envelope, "agent-native.effect-evidence/2")
        identity = completion["fact"]
        _require(identity["entity_type"] == definitions.ExternalFact.__entity_type__
                 and identity["generation"] == 1 and identity["collection_field"] is None, "creation_history_fact")
        fact_ref = Ref(EntityKey(identity["entity_type"], identity["entity_id"], 1))
        fact = values(fact_ref)
        application = values(Ref(EntityKey(definitions.FactApplication.__entity_type__, fact_ref.key.entity_id, 1)))
        external, operation = fact["external"], application["operation"]
        link, result = external["effect_link"], external["effect_result"]
        _require(application["fact"] == fact_ref and link is not None and result is not None
                 and link["effect"] == start_effect and link["operation_id"] == operation_id
                 and operation is not None and operation["fact"] == fact_ref
                 and operation["effect"] == start_effect and operation["operation_id"] == operation_id
                 and operation["evidence"] == evidence_ref, "creation_history_application")
        for binding in application["bindings"]:
            _require(binding["fact"] == fact_ref and binding["effect"] == start_effect
                     and binding["operation_id"] == operation_id, "creation_history_binding")
            if binding["target"] == v.resource_record(activity):
                return True
        if (result["evidence_kind"] == "confirmed" and result["proof"] != "readback_only"
                and operation["current_attempt"]
                and operation["state_before"] not in ("confirmed", "failed_no_effect", "cancelled_before_effect")):
            expected = resolve_operation_state(reader, definitions, external,
                state_before=operation["state_before"], current_attempt=operation["current_attempt"],
                evidence_prefix=evidence_prefix[:position])
            _require(expected == operation["state_after"], "creation_history_resolution")
            if expected == "confirmed":
                return True
    return False
