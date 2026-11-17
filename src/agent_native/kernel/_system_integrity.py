"""Callback-free static SYS7 provenance checks in the SAME accepted C1 graph.

No adapter issuer, author callback, updater, normalization, migration or SEND
authorization is reconstructed here. Historical facts stay historical.
"""
from hashlib import sha256

from agent_native.schema import EntityKey, Ref
from agent_native.state import strict_equal, thaw
from . import _command_values as v
from ._operation_proofs import OperationProofError, resolve_operation_state, no_effect_basis, has_creation_contradiction


class SystemIntegrityError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _require(condition, code):
    if not condition:
        raise SystemIntegrityError(code)


class SystemIntegrity:
    def __init__(self, definitions, registries, authority):
        registries = tuple(registries)
        self.d, self.authority = definitions, authority
        self.registries = {registry.namespace: registry for registry in registries}
        self.private_types = frozenset(d.definition_id for d in definitions.registry.descriptors(kind="entity"))
        self.runner = self.runtime = None
        self._optional_errors = ()
        if definitions.supports_agent_runtime:
            from ._runner_integrity import RunnerIntegrity, RunnerIntegrityError
            from ._runtime_integrity import RuntimeIntegrity, RuntimeIntegrityError
            self.runner = RunnerIntegrity(definitions, authority, self)
            self.runtime = RuntimeIntegrity(definitions, authority)
            self._optional_errors = (RunnerIntegrityError, RuntimeIntegrityError)
        # Optional composed profile: absence never imports skills implementation.
        # Presence invokes the REAL installed-schema validator; namespace equality
        # alone is not acceptance and missing code is not an import-nop fallback.
        self.skills = None
        self._skills_error = None
        if any(registry.namespace == "engine.skills" for registry in registries):
            if not definitions.supports_agent_runtime:
                raise SystemIntegrityError("unsupported_profile_registry")
            from agent_native.skills._integrity import SkillsIntegrity, SkillsIntegrityError
            try:
                self.skills = SkillsIntegrity(definitions, registries, authority)
            except SkillsIntegrityError as error:
                raise SystemIntegrityError(error.code) from None
            self._skills_error = SkillsIntegrityError
        self.immutable = {definitions.ExternalFact.__entity_type__,
                          definitions.FactApplication.__entity_type__,
                          definitions.EffectEvidence.__entity_type__,
                          definitions.Invocation.__entity_type__}

    def _ref(self, model, ident):
        return Ref(EntityKey(model.__entity_type__, ident, 1))

    def _values(self, reader, ref):
        _require(reader.exists(ref), "missing_private_proof")
        return thaw(reader.resolve(ref).values)

    def _identity(self, item, *, ordinary=True):
        ident = self.authority.identity(item)
        if ordinary:
            _require(ident[0] not in self.private_types,
                     "private_domain_identity")
        return ident

    def _origin(self, reader, origin):
        identity = self.authority.identity(origin["activity"], entity_only=True)
        return self.authority.check_activity_occurrence(reader, Ref(EntityKey(*identity[:3])),
            original_anchor=origin["anchor"], start_invocation=origin["start_invocation"],
            start_operation_grant=origin["start_operation_grant"])

    @staticmethod
    def _payload(payload):
        _require(type(payload["format"]) is str and bool(payload["format"]), "empty_payload_format")
        try:
            return v.open_envelope(payload, payload["format"])
        except (TypeError, ValueError, UnicodeError):
            raise SystemIntegrityError("invalid_protocol_payload") from None

    @staticmethod
    def _name(value):
        try:
            valid = type(value) is str and bool(value) and len(value.encode("utf-8")) <= 256
        except UnicodeError:
            valid = False
        _require(valid, "no_effect_identifier")

    def _no_effect_rule(self, rule, activity=None):
        self.d.registry.check_accepted_record(self.d.NoEffectActivityRuleRecord.__record_type__, rule)
        for value in (rule["slot"], rule["terminal_state"], *rule["creation_pending_states"]):
            self._name(value)
        states = rule["creation_pending_states"]
        _require(0 < len(states) <= 16 and len(set(states)) == len(states), "no_effect_pending_states")
        _require((rule["pointer_slot"] is None) == (rule["pointer_field"] is None), "no_effect_pointer_rule")
        pointer_fields = rule["pointer_binding_fields"]
        _require(len(pointer_fields) in (0, 4) and len(set(pointer_fields)) == len(pointer_fields),
                 "no_effect_pointer_binding_fields")
        for field in pointer_fields:
            self._name(field)
        if rule["pointer_slot"] is not None:
            self._name(rule["pointer_slot"]); self._name(rule["pointer_field"])
        else:
            _require(pointer_fields == (), "no_effect_unconfigured_pointer_binding")
        if activity is not None:
            spec = self.authority.activities.get(activity.key.entity_type)
            _require(spec is not None and rule["terminal_state"] in spec[3]
                     and set(states).issubset(spec[1]) and not set(states).intersection(spec[3]),
                     "no_effect_activity_states")
            field = self.authority.descriptors[activity.key.entity_type].json_schema["properties"][spec[0]]
            if "enum" in field:
                _require(set(states).issubset(field["enum"]), "no_effect_pending_literal")

    def _no_effect_origin(self, reader, origin, rule, link):
        self.d.registry.check_accepted_record(self.d.NoEffectActivityOrigin.__record_type__, origin)
        _require(link is not None and origin["start_effect"] == link["effect"]
                 and origin["target_slot"] == rule["slot"] and origin["resource"] == link["resource"],
                 "no_effect_origin_link")
        check = self.authority.check_new_activity_start(reader, origin["start_effect"])
        expected = {"activity": v.resource_record(check.activity), "anchor": check.original_anchor,
            "start_invocation": check.start_invocation, "start_operation_grant": check.start_operation_grant}
        _require(check.start_eligibility == "single_start" and strict_equal(origin["original"], expected),
                 "no_effect_original_start")
        self._no_effect_rule(rule, check.activity)
        return check

    def _operation_prefix(self, reader, operation, link):
        identifiers = self._values(reader, link["effect"])["evidence_ids"]
        completion = operation["evidence"]
        _require(completion.key.entity_type == self.d.EffectEvidence.__entity_type__
                 and completion.key.generation == 1
                 and identifiers.count(completion.key.entity_id) == 1, "operation_completion_history")
        return identifiers[:identifiers.index(completion.key.entity_id)]

    def _no_effect_history(self, reader, prefix, activity, start_effect, operation_id):
        _require(len(prefix) <= self.authority.limits.max_work, "no_effect_history_quota")
        try:
            conflict = has_creation_contradiction(reader, self.d, activity=activity, start_effect=start_effect,
                operation_id=operation_id, evidence_prefix=prefix)
        except OperationProofError as error:
            raise SystemIntegrityError(error.code) from None
        _require(not conflict, "no_effect_contradictory_creation")

    def _no_effect_application(self, reader, fact_ref, fact, item, proof):
        self.d.registry.check_accepted_record(self.d.NoEffectActivityProof.__record_type__, proof)
        rule, origin = fact["admission"]["output_binding"]["no_effect_activity_rule"], fact["admission"]["no_effect_origin"]
        link, result, operation = fact["external"]["effect_link"], fact["external"]["effect_result"], item["operation"]
        _require(rule is not None and origin is not None and item["terminal"] is None and operation is not None,
                 "no_effect_proof_scope")
        check = self._no_effect_origin(reader, origin, rule, link)
        _require(proof["fact"] == fact_ref and proof["original"] == origin["original"]
                 and proof["start_effect"] == link["effect"] and proof["operation_id"] == link["operation_id"]
                 and proof["resource"] == link["resource"] and proof["desired_generation"] == link["desired_generation"]
                 and proof["claim_fence"] == link["claim_fence"] and proof["claim_witness"] == link["claim_witness"]
                 and proof["claim_witness_digest"] == link["claim_witness_digest"]
                 and proof["terminal_state"] == rule["terminal_state"] and result["evidence_kind"] == "failed_no_effect"
                 and operation["current_attempt"] and operation["state_after"] == "failed_no_effect", "no_effect_proof_link")
        prefix = self._operation_prefix(reader, operation, link)
        try:
            basis = no_effect_basis(reader, self.d, fact["external"], state_before=operation["state_before"],
                current_attempt=operation["current_attempt"], evidence_prefix=prefix)
        except OperationProofError as error:
            raise SystemIntegrityError(error.code) from None
        _require(basis is not None and proof["basis"] == basis, "no_effect_fresh_basis")
        self._no_effect_history(reader, prefix, check.activity, link["effect"], link["operation_id"])
        self._no_effect_local_shape(reader, check, origin, rule, proof, fact["admission"]["targets"])

    def _no_effect_local_shape(self, reader, check, origin, rule, proof, targets):
        local = proof["local_eligibility"]
        _require(local["activity"] == origin["original"]["activity"]
                 and local["helper_disposition"] == proof["disposition"], "no_effect_local_identity")
        fields = local["binding_fields"]
        _require(len(fields) == 4 and len(set(fields)) == 4, "no_effect_binding_fields")
        descriptor = self.authority.descriptors[check.activity.key.entity_type]
        for name in fields:
            self._name(name)
            field = next((field for field in descriptor.fields if field.name == name), None)
            schema = descriptor.json_schema.get("properties", {}).get(name, {})
            alternatives = schema.get("anyOf", ())
            _require(field is not None and field.relation is None
                     and {part.get("type") for part in alternatives} == {"string", "null"}, "no_effect_binding_field_type")
        target = next((target for target in targets if target["slot"] == rule["slot"]), None)
        _require(target is not None, "no_effect_target_slot")
        if local["existence_before"] == "absent":
            _require(local["state_before"] is None and local["backlink_before"] is None
                     and local["binding_values_before"] == () and local["binding_digest_before"] is None
                     and proof["disposition"] == "historical_anchor_only"
                     and proof["pointer_target"] is None and not proof["pointer_cleared"], "no_effect_absent_snapshot")
        else:
            spec = self.authority.activities[check.activity.key.entity_type]
            _require(local["backlink_before"] == check.original_anchor
                     and local["binding_values_before"] == (None,) * 4
                     and local["binding_digest_before"] == v.digest(tuple(zip(fields, local["binding_values_before"])))
                     and target["target"] == origin["original"]["activity"] and target["remote_object"] is None
                     and target["binding_digest"] == local["binding_digest_before"], "no_effect_present_snapshot")
            _require((proof["disposition"] == "ended" and local["state_before"] in rule["creation_pending_states"])
                     or (proof["disposition"] == "already_terminal" and local["state_before"] in spec[3]),
                     "no_effect_local_disposition")
        pointer = proof["pointer_target"]
        if rule["pointer_slot"] is None:
            _require(pointer is None and not proof["pointer_cleared"], "no_effect_unconfigured_pointer")
        else:
            claim = next((target for target in targets if target["slot"] == rule["pointer_slot"]), None)
            _require(claim is not None, "no_effect_pointer_claim")
            if pointer is not None:
                _require(pointer == claim["target"] and pointer["collection_field"] is None, "no_effect_pointer_target")
            target = claim["target"]
            if target is not None:
                self._identity(target)
                _require(target["collection_field"] is None, "no_effect_pointer_entity")
                descriptor = self.authority.descriptors[target["entity_type"]]
                field = next((field for field in descriptor.fields if field.name == rule["pointer_field"]), None)
                relation = None if field is None else field.relation
                _require(relation is not None and relation.kind == "ref" and not relation.many and relation.nullable
                         and relation.target_type == check.activity.key.entity_type, "no_effect_pointer_type")
                for name in rule["pointer_binding_fields"]:
                    field = next((field for field in descriptor.fields if field.name == name), None)
                    schema = descriptor.json_schema.get("properties", {}).get(name, {})
                    alternatives = schema.get("anyOf", ())
                    _require(field is not None and field.relation is None and (schema.get("type") == "string"
                        or {part.get("type") for part in alternatives} == {"string", "null"}), "no_effect_pointer_binding_type")
        _require(not proof["pointer_cleared"] or pointer is not None and local["existence_before"] == "present",
                 "no_effect_pointer_decision")

    def _no_effect_before(self, base, staged, proof, rule, admission):
        local, original = proof["local_eligibility"], proof["original"]
        identity = self.authority.identity(local["activity"], entity_only=True)
        activity = Ref(EntityKey(*identity[:3]))
        present = base.exists(activity)
        _require(present == (local["existence_before"] == "present"), "no_effect_before_existence")
        if present:
            spec = self.authority.activities[identity[0]]
            values = base.resolve(activity).values
            fields = local["binding_fields"]
            _require(values[spec[0]] == local["state_before"] and values[spec[2]] == local["backlink_before"]
                     and tuple(values[field] for field in fields) == local["binding_values_before"]
                     and v.digest(tuple((field, thaw(values[field])) for field in fields)) == local["binding_digest_before"],
                     "no_effect_before_snapshot")
            state = proof["terminal_state"] if proof["disposition"] == "ended" else local["state_before"]
            _require(staged.exists(activity) and staged.field(activity, spec[0]) == state, "no_effect_domain_projection")
        else:
            _require(not staged.exists(activity), "no_effect_resurrection")
        _require(staged.field(original["anchor"], "revoked"), "no_effect_anchor_projection")
        expected_pointer, target, before_pointer = None, None, None
        if rule["pointer_slot"] is not None:
            claim = next((target for target in admission["targets"] if target["slot"] == rule["pointer_slot"]), None)
            _require(claim is not None, "no_effect_pointer_claim")
            if claim["target"] is not None:
                candidate = Ref(EntityKey(*self.authority.identity(claim["target"], entity_only=True)[:3]))
                if base.exists(candidate):
                    target = candidate
                    values = base.resolve(candidate).values
                    before_pointer = values[rule["pointer_field"]]
                    current_digest = v.digest(tuple((field, thaw(values[field])) for field in rule["pointer_binding_fields"]))
                    if present and current_digest == claim["binding_digest"]:
                        expected_pointer = claim["target"]
        _require(proof["pointer_target"] == expected_pointer, "no_effect_pointer_availability")
        cleared = expected_pointer is not None and before_pointer == activity
        _require(proof["pointer_cleared"] == cleared, "no_effect_pointer_snapshot")
        if target is not None:
            expected_value = None if cleared else before_pointer
            _require(staged.exists(target) and staged.field(target, rule["pointer_field"]) == expected_value,
                     "no_effect_pointer_projection")

    def _prerequisite(self, reader, value, origin, resource):
        _require(origin is not None and strict_equal(value["original"], origin)
                 and strict_equal(value["resource"], resource), "stop_prerequisite_origin")
        self._origin(reader, origin)
        target = value["target"]
        _require(target["target"] is not None and target["binding_digest"] is not None
                 and target["remote_object"] is not None, "unbound_stop_target")
        _require(strict_equal(target["target"], origin["activity"]), "stop_target_occurrence")
        _require(bool(value["binding_fields"]) and len(set(value["binding_fields"])) == len(value["binding_fields"]),
                 "stop_binding_fields")
        adapter = value["adapter"]
        _require(value["targeting"] == adapter["stop_targeting"]
                 and (value["targeting"] != "global_exclusive" or adapter["remote_exclusive"]),
                 "unsupported_stop_targeting")
        _require(target["remote_object"]["remote_identity"] == adapter["remote_identity"],
                 "stop_remote_identity")

    def _intent(self, reader, ref, item):
        origin, prerequisite = item["stop_origin"], item["stop_prerequisite"]
        if origin is not None:
            self._origin(reader, origin)
        if prerequisite is not None:
            self._prerequisite(reader, prerequisite, origin, item["resource"])
        reason = item["local_no_send_reason"]
        _require(reason is None or item["state"] == "cancelled_before_effect", "local_no_send_not_cancelled")
        _require(item["state"] != "pending" or prerequisite is None, "pending_has_stop_prerequisite")

    def _evidence(self, reader, ref, item):
        intent = self._values(reader, item["effect"])
        _require(item["operation_id"] == intent["operation_id"]
                 and item["desired_generation"] == intent["desired_generation"]
                 and intent["claim_fence"] is not None and item["claim_fence"] <= intent["claim_fence"],
                 "evidence_intent_link")
        payload = item["payload"]
        witness = self._payload(payload)
        if payload["format"] == "agent-native.effect-claim/2":
            self.d.registry.check_accepted_record(self.d.EffectClaimWitness.__record_type__, witness)
            _require(witness["capability_digest"] == v.digest(witness["capability"])
                     and witness["payload_digest"] == v.digest(self._payload(intent["payload"]))
                     and witness["resource"] == intent["resource"]
                     and witness["stop_prerequisite"] == intent["stop_prerequisite"]
                     and item["source"] is None and item["source_sequence"] is None,
                     "claim_static_integrity")
        elif payload["format"] == "agent-native.effect-evidence/2":
            self.d.registry.check_accepted_record(self.d.EffectCompletionWitness.__record_type__, witness)
            identity = self.authority.identity(witness["fact"], entity_only=True)
            _require(identity[0] == self.d.ExternalFact.__entity_type__ and identity[2] == 1,
                     "completion_fact_identity")
            fact = self._values(reader, Ref(EntityKey(*identity[:3])))
            external = fact["external"]
            link, result = external["effect_link"], external["effect_result"]
            _require(link is not None and result is not None
                     and link["effect"] == item["effect"] and link["operation_id"] == item["operation_id"]
                     and link["desired_generation"] == item["desired_generation"]
                     and link["claim_fence"] == item["claim_fence"]
                     and witness["proof"] == result["proof"] and witness["mode"] == link["mode"]
                     and witness["resource"] == intent["resource"]
                     and item["kind"] == result["evidence_kind"]
                     and item["source"] == external["identity"]["source"]
                     and item["source_sequence"] == (None if external["observation"] is None else
                                                       external["observation"]["source_sequence"]),
                     "completion_static_integrity")
            fact_ref = Ref(EntityKey(*identity[:3]))
            application = self._values(reader, self._ref(self.d.FactApplication, fact_ref.key.entity_id))
            operation = application["operation"]
            _require(application["fact"] == fact_ref and operation is not None
                     and operation["fact"] == fact_ref and operation["evidence"] == ref
                     and operation["effect"] == item["effect"] and operation["operation_id"] == item["operation_id"]
                     and operation["claim_fence"] == item["claim_fence"]
                     and operation["desired_generation"] == item["desired_generation"], "completion_application_link")
        else:
            raise SystemIntegrityError("unsupported_evidence_format")

    def _fact(self, reader, ref, item):
        external, admission = item["external"], item["admission"]
        identity, adapter = external["identity"], external["adapter"]
        _require(ref.key.generation == 1 and ref.key.entity_id == v.digest(identity), "fact_identity")
        _require(item["external_sha256"] == v.digest(external), "fact_external_digest")
        _require(item["envelope_sha256"] == v.digest({key: value for key, value in item.items()
                                                     if key != "envelope_sha256"}), "fact_envelope_digest")
        _require(identity["adapter_id"] == adapter["adapter_id"]
                 and identity["remote_identity"] == adapter["remote_identity"]
                 and identity["source"] == adapter["source"]
                 and identity["source_epoch"] == adapter["source_epoch"], "fact_adapter_identity")
        # Endpoint source provenance survives transport-only terminal results.
        # An ordering group belongs only to an actual observation witness.
        _require((identity["source"] is None) == (identity["source_epoch"] is None), "fact_source_pair")
        _require(external["producer_schema"] == admission["output_binding"]["output_schema"],
                 "fact_output_schema")
        self._payload(external["original_payload"])
        rule = admission["output_binding"]["no_effect_activity_rule"]
        origin = admission["no_effect_origin"]
        creation = None
        if rule is None:
            _require(origin is None, "no_effect_origin_without_rule")
        else:
            self._no_effect_rule(rule)
            link = external["effect_link"]
            if link is None:
                _require(origin is None, "observation_no_effect_origin")
            else:
                creation = self.authority.check_new_activity_start(reader, link["effect"])
                self._no_effect_rule(rule, creation.activity)
                if creation.start_eligibility == "multiple_effects":
                    _require(origin is None, "multiple_effects_no_effect_origin")
                else:
                    _require(origin is not None, "missing_no_effect_origin")
                    creation = self._no_effect_origin(reader, origin, rule, link)
        targets = admission["targets"]
        _require(len(targets) <= 8 and len({t["slot"] for t in targets}) == len(targets), "fact_targets")
        if origin is not None:
            claim = next((target for target in targets if target["slot"] == origin["target_slot"]), None)
            _require(claim is not None and (claim["target"] is None
                     or claim["target"] == origin["original"]["activity"]), "no_effect_origin_target")
        for target in targets:
            if target["target"] is None:
                historical = (origin is not None and creation is not None and creation.disposition == "absent"
                    and target["slot"] == origin["target_slot"] and target["binding_digest"] is None
                    and target["remote_object"] is None)
                _require(historical or target["binding_digest"] is None and target["remote_object"] is not None,
                         "unmapped_target_shape")
            else:
                self._identity(target["target"])
                _require(target["binding_digest"] is not None, "missing_target_binding")
        if external["observation"] is not None:
            _require(identity["source"] is not None and identity["ordering_group"] is not None
                     and external["observation"]["remote_subject"]["remote_identity"] == identity["remote_identity"],
                     "observation_source_identity")
        else:
            _require(identity["ordering_group"] is None, "unobserved_ordering_group")
        if admission["observation"] is not None:
            _require(external["observation"] is not None
                     and admission["observation"]["cursor_key"]["ordering_group"] == identity["ordering_group"]
                     and admission["observation"]["cursor_key"]["remote_subject"] ==
                         external["observation"]["remote_subject"], "local_observation_claim")
        origin = admission["activity_origin"]
        if origin is not None:
            self._origin(reader, origin)
        link, result = external["effect_link"], external["effect_result"]
        if identity["variant"] == "observation":
            _require(link is None and result is None and external["observation"] is not None
                     and identity["operation_id"] is None and identity["claim_fence"] is None
                     and identity["mode"] is None and identity["dispatch"] is None and origin is None, "observation_variant")
        else:
            _require(link is not None and result is not None, "effect_variant")
            _require(identity["dispatch"] == link["dispatch"]
                     and identity["operation_id"] == link["operation_id"]
                     and identity["claim_fence"] == link["claim_fence"]
                     and identity["mode"] == link["mode"], "effect_identity_link")
            intent = self._values(reader, link["effect"])
            _require(intent["invocation"] == link["invocation"] and intent["grant"] == link["operation_grant"]
                     and intent["operation_id"] == link["operation_id"]
                     and intent["desired_generation"] == link["desired_generation"]
                     and intent["resource"] == link["resource"] and intent["action"] == link["effect_action"],
                     "fact_intent_link")
            _require(intent["stop_origin"] == origin, "fact_stop_origin")
            _require(link["input_digest"] == v.digest(self._payload(intent["payload"])), "fact_input_digest")
            claim = self._values(reader, link["claim_witness"])
            _require(claim["effect"] == link["effect"] and claim["operation_id"] == link["operation_id"]
                     and claim["claim_fence"] == link["claim_fence"]
                     and claim["desired_generation"] == link["desired_generation"]
                     and v.digest(claim) == link["claim_witness_digest"], "fact_claim_link")
            _require(claim["payload"]["format"] == "agent-native.effect-claim/2", "unsupported_claim_format")
            witness = self._payload(claim["payload"])
            self.d.registry.check_accepted_record(self.d.EffectClaimWitness.__record_type__, witness)
            _require(witness["capability_digest"] == link["claim_capability_digest"]
                     and witness["capability_digest"] == v.digest(witness["capability"])
                     and witness["payload_digest"] == link["input_digest"]
                     and witness["resource"] == link["resource"], "claim_witness_integrity")
            if link["mode"] == "execute":
                _require(witness["capability"] == adapter and link["request_action"] == link["effect_action"],
                         "execution_adapter_changed")
            else:
                _require(witness["capability"]["remote_identity"] == adapter["remote_identity"],
                         "reconcile_account_changed")
            _require(link["request_action"] in adapter["actions"], "request_action_unsupported")
            if result["proof"] in ("operation_terminal", "remote_fence") and link["mode"] == "reconcile":
                _require(adapter["terminal_reconciliation"], "unproven_terminal_reconciliation")
            if result["observed_generation_claim"] is not None:
                _require(external["observation"] is not None
                         and external["observation"]["source_sequence"] is not None,
                         "observed_claim_without_source")
            if result["causal_claim"] is not None:
                _require(result["causal_claim"]["operation_id"] == link["operation_id"]
                         and result["causal_claim"]["desired_generation"] == link["desired_generation"],
                         "causal_claim_mismatch")

    def _output(self, envelope, binding):
        _require(envelope["schema_identity"] == binding["output_schema"], "accepted_output_schema")
        raw = envelope["record_json"].encode("utf-8")
        _require(len(raw) <= 128 * 1024 and sha256(raw).hexdigest() == envelope["sha256"],
                 "accepted_output_digest_or_size")
        schema = envelope["schema_identity"]
        registry = self.registries.get(schema["namespace"])
        _require(registry is not None, "accepted_output_registry")
        descriptor = registry.record_descriptor(schema["definition_id"])
        _require(registry.revision == schema["registry_revision"]
                 and descriptor.revision == schema["definition_revision"]
                 and descriptor.fingerprint == schema["fingerprint"], "accepted_output_identity")
        registry.decode_accepted_record(raw)

    def _application(self, reader, ref, item):
        fact_ref = item["fact"]
        fact = self._values(reader, fact_ref)
        _require(ref.key.generation == 1 and ref.key.entity_id == fact_ref.key.entity_id
                 and item["original_envelope_sha256"] == fact["envelope_sha256"]
                 and item["output_binding"] == fact["admission"]["output_binding"], "application_identity")
        if item["accepted_output"] is not None:
            self._output(item["accepted_output"], item["output_binding"])
        _require(len(item["bindings"]) <= 8 and len({binding["slot"] for binding in item["bindings"]}) ==
                 len(item["bindings"]), "application_binding_slots")
        _require((item["observation"] is not None) == (item["observation_status"] == "advanced"),
                 "application_observation_status")
        operation = item["operation"]
        link = fact["external"]["effect_link"]
        if item["accepted_output"] is not None and link is not None:
            _require(operation is not None, "missing_effect_operation")
        if item["accepted_output"] is None:
            _require(operation is None and not item["bindings"] and item["observation"] is None
                     and item["terminal"] is None and item["no_effect_activity"] is None,
                     "rejected_output_consequence")
        if operation is not None:
            _require(link is not None and operation["fact"] == fact_ref
                     and operation["effect"] == link["effect"] and operation["operation_id"] == link["operation_id"]
                     and operation["claim_fence"] == link["claim_fence"]
                     and operation["desired_generation"] == link["desired_generation"]
                     and operation["proof"] == fact["external"]["effect_result"]["proof"], "application_operation")
            evidence = self._values(reader, operation["evidence"])
            result = fact["external"]["effect_result"]
            _require(evidence["kind"] == result["evidence_kind"], "completion_result_kind")
            prefix = self._operation_prefix(reader, operation, link)
            try:
                expected = resolve_operation_state(reader, self.d, fact["external"],
                    state_before=operation["state_before"], current_attempt=operation["current_attempt"],
                    evidence_prefix=prefix)
            except OperationProofError as error:
                raise SystemIntegrityError(error.code) from None
            _require(operation["state_after"] == expected, "operation_resolution_transition")
        for binding in item["bindings"]:
            target = next((claim for claim in fact["admission"]["targets"] if claim["slot"] == binding["slot"]), None)
            _require(link is not None and binding["fact"] == fact_ref
                     and binding["effect"] == link["effect"] and binding["operation_id"] == link["operation_id"]
                     and target is not None and target["target"] == binding["target"]
                     and target["binding_digest"] == binding["prior_binding_digest"],
                     "application_binding")
        advance = item["observation"]
        if advance is not None:
            claim, observation = fact["admission"]["observation"], fact["external"]["observation"]
            identity = fact["external"]["identity"]
            _require(claim is not None and observation is not None and advance["fact"] == fact_ref
                     and advance["cursor_key"] == claim["cursor_key"]
                     and advance["selection_generation"] == claim["selection_generation"]
                     and advance["sequence_after"] == observation["source_sequence"]
                     and advance["proof"] == observation["proof"]
                     and advance["source"] == {key: identity[key] for key in
                                              ("adapter_id", "remote_identity", "source", "source_epoch")},
                     "application_observation")
            _require(advance["sequence_before"] is None or
                     advance["sequence_after"] > advance["sequence_before"], "observation_not_advanced")
            _require((advance["causal_effect"] is None) == (advance["causal_generation"] is None),
                     "observation_causal_pair")
            for permission in advance["fields"]:
                target = next((target for target in fact["admission"]["targets"]
                               if target["slot"] == permission["slot"]), None)
                _require(target is not None and target["target"] == permission["target"],
                         "observation_field_target")
        terminal = item["terminal"]
        if terminal is not None:
            result = fact["external"]["effect_result"]
            _require(link is not None and result["evidence_kind"] == "confirmed"
                     and result["proof"] != "readback_only"
                     and terminal["fact"] == fact_ref
                     and terminal["original"] == fact["admission"]["activity_origin"]
                     and terminal["stop_effect"] == link["effect"]
                     and terminal["stop_operation_id"] == link["operation_id"]
                     and terminal["stop_resource"] == link["resource"]
                     and terminal["stop_generation"] == link["desired_generation"], "application_terminal")
            self._origin(reader, terminal["original"])
            stop = self._values(reader, terminal["stop_effect"])
            _require(stop["stop_prerequisite"] is not None and
                     stop["stop_prerequisite"]["target"]["remote_object"] == terminal["remote_occurrence"],
                     "terminal_remote_occurrence")

        negative = item["no_effect_activity"]
        rule = fact["admission"]["output_binding"]["no_effect_activity_rule"]
        origin = fact["admission"]["no_effect_origin"]
        if (rule is not None and origin is not None and item["accepted_output"] is not None
                and operation is not None and operation["current_attempt"]
                and fact["external"]["effect_result"]["evidence_kind"] == "failed_no_effect"):
            self._no_effect_origin(reader, origin, rule, link)
            try:
                required_basis = no_effect_basis(reader, self.d, fact["external"],
                    state_before=operation["state_before"], current_attempt=operation["current_attempt"],
                    evidence_prefix=self._operation_prefix(reader, operation, link))
            except OperationProofError as error:
                raise SystemIntegrityError(error.code) from None
            _require(required_basis is None or negative is not None, "missing_no_effect_activity_proof")
        if negative is not None:
            self._no_effect_application(reader, fact_ref, fact, item, negative)

    def _cursor(self, reader, ref, item):
        _require(ref.key.generation == 1 and ref.key.entity_id == v.digest(item["key"]), "cursor_identity")
        self._identity(item["key"]["owner"])
        _require(item["selected_source"]["remote_identity"] == item["key"]["remote_subject"]["remote_identity"],
                 "cursor_remote_identity")
        if item["state"] == "awaiting_resync":
            _require(item["sequence"] is None and item["last_application"] is None, "unready_cursor")
        else:
            _require(item["sequence"] is not None and item["last_application"] is not None, "ready_cursor")
            app = self._values(reader, item["last_application"])
            advance = app["observation"]
            _require(advance is not None and advance["cursor_key"] == item["key"]
                     and advance["source"] == item["selected_source"]
                     and advance["sequence_after"] == item["sequence"]
                     and advance["selection_generation"] == item["selection_generation"], "cursor_application")

    def _check(self, reader, ref):
        record = reader.resolve(ref)
        item = thaw(record.values)
        typ = ref.key.entity_type
        if typ in (self.d.ExternalFact.__entity_type__, self.d.FactApplication.__entity_type__,
                   self.d.ObservationCursor.__entity_type__):
            _require(record.partition == "durable", "private_fact_must_be_durable")
        if typ == self.d.ExternalFact.__entity_type__:
            self._fact(reader, ref, item)
        elif typ == self.d.FactApplication.__entity_type__:
            self._application(reader, ref, item)
        elif typ == self.d.ObservationCursor.__entity_type__:
            self._cursor(reader, ref, item)
        elif typ == self.d.EffectIntent.__entity_type__:
            self._intent(reader, ref, item)
        elif typ == self.d.EffectEvidence.__entity_type__:
            self._evidence(reader, ref, item)

    def validate_graph(self, reader):
        for model in (self.d.EffectIntent, self.d.EffectEvidence, self.d.ExternalFact,
                      self.d.FactApplication, self.d.ObservationCursor):
            for ref in reader.entities(model.__entity_type__):
                self._check(reader, ref)

        try:
            if self.runner is not None:
                self.runner.validate_graph(reader)
                self.runtime.validate_graph(reader)
        except self._optional_errors as error:
            raise SystemIntegrityError(error.code) from None
        # This existing graph path is also the production startup/restore guard.
        # B.validate_restore is its graph alias, so do not invent a second caller
        # or run the same retained graph twice during restore.
        if self.skills is not None:
            try:
                self.skills.validate_graph(reader)
            except self._skills_error as error:
                raise SystemIntegrityError(error.code) from None

    def validate_transition(self, base, staged, *, local_attestation=None):
        before, after = base._data().records, staged._data().records
        for key in before.keys() - after.keys():
            _require(key.entity_type not in self.immutable, "private_history_deletion")
        for key, record in after.items():
            previous = before.get(key)
            if previous is record:
                continue
            if previous is not None:
                old, new = thaw(previous.values), thaw(record.values)
                if key.entity_type in self.immutable:
                    _require(strict_equal(old, new), "private_history_rewrite")
                elif key.entity_type == self.d.EffectIntent.__entity_type__:
                    for name in ("invocation", "grant", "resource", "action", "payload", "operation_id",
                                 "desired_generation", "stop_origin"):
                        _require(strict_equal(old[name], new[name]), "intent_header_rewrite")
                    if old["stop_prerequisite"] is not None:
                        _require(strict_equal(old["stop_prerequisite"], new["stop_prerequisite"]),
                                 "stop_prerequisite_rewrite")
                elif key.entity_type == self.d.ObservationCursor.__entity_type__:
                    _require(old["key"] == new["key"] and new["selection_generation"] >= old["selection_generation"],
                             "cursor_selection_reversed")
                    if new["selection_generation"] == old["selection_generation"]:
                        _require(new["selected_source"] == old["selected_source"], "cursor_source_without_selection")
                        if old["sequence"] is not None:
                            _require(new["sequence"] is not None and new["sequence"] >= old["sequence"],
                                     "cursor_sequence_reversed")
            self._check(staged, Ref(key))
            if key.entity_type == self.d.FactApplication.__entity_type__ and previous is None:
                item = thaw(record.values)
                if item["no_effect_activity"] is not None:
                    fact = self._values(staged, item["fact"])
                    self._no_effect_before(base, staged, item["no_effect_activity"],
                        fact["admission"]["output_binding"]["no_effect_activity_rule"], fact["admission"])

        try:
            if self.runner is not None:
                self.runner.validate_transition(base, staged, local_attestation=local_attestation)
                self.runtime.validate_transition(base, staged)
        except self._optional_errors as error:
            raise SystemIntegrityError(error.code) from None
        if self.skills is not None:
            try:
                self.skills.validate_transition(base, staged)
            except self._skills_error as error:
                raise SystemIntegrityError(error.code) from None
